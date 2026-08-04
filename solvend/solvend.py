#!/usr/bin/env python3
"""SolVend state machine — deterministic core. Stdlib only.

Deploy to /opt/solvend/solvend.py. Three callers share it:
  - the solana-pay-invoice skill   -> `invoice <item> <amount> <channel> <handle>`
  - the payment-watcher SOP (cron) -> `watch`
  - the ESP32 serial daemon        -> `claim <otp>`

Design rule: the LLM never decides whether a payment is valid, never mints an
OTP, and never performs a state transition. It reads shaped JSON out of here
and writes chat messages. Everything below the chat layer is SQL and integer
comparisons, so a prompt-injected model cannot talk its way into a dispense.

State machine:
  AWAITING_PAYMENT --(validated on-chain transfer)--> PAID_UNCLAIMED  (+OTP, +15min)
  PAID_UNCLAIMED   --(correct OTP at keypad)-------->  CLAIMED
  PAID_UNCLAIMED   --(otp_expires_at passed)-------->  PAID_EXPIRED
  PAID_EXPIRED     --(customer asks, sold out)------>  EXPIRED_REFUND_REQUESTED
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from secrets import randbelow

# Mainnet USDC by default. Override for devnet rehearsals:
#   SOLVEND_USDC_MINT=4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU   (devnet USDC)
# Test the whole loop on devnet with faucet money, then change two lines in
# /etc/solvend/env to go live. Nothing else in the system knows the difference.
USDC_MINT = os.environ.get("SOLVEND_USDC_MINT",
                           "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
USDC_DECIMALS = 6

# Single source of truth for the catalogue. Price is NOT a parameter anywhere in
# this file — same control as the refund destination. A model injected into
# invoicing a cola at 0.01 cannot: cmd_invoice looks the price up here and an
# unknown item is rejected outright. Keep the display prices in
# skills/solana-pay-invoice/SKILL.md in sync with these; this table is what
# actually charges, the skill text only tells the model what to say.
# `slot` is the physical gantry position the ESP32 understands.
ITEMS = {
    "water":  {"price_base": 1_000_000, "slot": "drink-1"},
    "cola":   {"price_base": 1_500_000, "slot": "drink-2"},
    "energy": {"price_base": 2_500_000, "slot": "drink-3"},
}
OTP_TTL_SECS = 15 * 60
MAX_OTP_ATTEMPTS = 5          # per invoice, then the OTP is dead
RPC_TIMEOUT_SECS = 10
SIG_LOOKBACK = 10             # signatures per reference per poll

DB_PATH = os.environ.get("SOLVEND_DB", "/var/lib/solvend/solvend.db")

# Never in code, never in a skill body, never in the model's context.
# systemd: EnvironmentFile=/etc/solvend/env  (chmod 600, root:zeroclaw)
RPC_URL = os.environ.get("SOLVEND_RPC_URL", "https://api.mainnet-beta.solana.com")
MERCHANT = os.environ.get("SOLVEND_RECIPIENT", "MERCHANT_WALLET_PUBKEY_HERE")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  invoice_id     TEXT PRIMARY KEY,
  reference      TEXT NOT NULL UNIQUE,
  item           TEXT NOT NULL,
  amount_base    INTEGER NOT NULL,          -- micro-USDC; never a float
  channel        TEXT NOT NULL,
  handle         TEXT NOT NULL,
  status         TEXT NOT NULL,
  otp            TEXT,
  otp_attempts   INTEGER NOT NULL DEFAULT 0,
  signature      TEXT UNIQUE,               -- one settling tx can never pay two invoices
  created_at     INTEGER NOT NULL,
  paid_at        INTEGER,
  otp_expires_at INTEGER,
  claimed_at     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_status ON invoices(status);
CREATE TABLE IF NOT EXISTS seq (n INTEGER NOT NULL);
INSERT INTO seq (n) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM seq);
-- Two live OTPs must never collide, or one customer's code opens another's slot.
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_otp
  ON invoices(otp) WHERE status = 'PAID_UNCLAIMED';
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# RPC
# --------------------------------------------------------------------------
def rpc(method: str, params: list, _transport=None) -> dict:
    """_transport is injected by tests. No live network in the test suite."""
    if _transport is not None:
        return _transport(method, params)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC_URL, data=body.encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_SECS) as resp:
        return json.loads(resp.read())


def validate_transfer(tx: dict, amount_base: int) -> bool:
    """True only if this transaction actually moved >= amount_base USDC to MERCHANT.

    A signature on the reference key proves nothing: the reference is a public
    read-only account and anyone can attach it to any transaction. So we ignore
    the reference entirely here and read the merchant's own token balance delta
    out of transaction metadata. Balance deltas cannot be spoofed by instruction
    shape, inner-instruction nesting, or CPI tricks the way naive instruction
    parsing can.
    """
    if not tx:
        return False
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:          # a failed tx still gets a signature
        return False

    def merchant_usdc(entries):
        for e in entries or []:
            if e.get("mint") == USDC_MINT and e.get("owner") == MERCHANT:
                return int(e["uiTokenAmount"]["amount"])
        return 0

    delta = merchant_usdc(meta.get("postTokenBalances")) - merchant_usdc(
        meta.get("preTokenBalances")
    )
    return delta >= amount_base           # overpayment settles; underpayment does not


def check_reference(ref: str, amount_base: int, _transport=None):
    """-> settling signature, or None. Two RPC calls, only for open invoices."""
    sigs = rpc(
        "getSignaturesForAddress",
        [ref, {"limit": SIG_LOOKBACK, "commitment": "finalized"}],
        _transport,
    ).get("result") or []

    for entry in sigs:
        if entry.get("err") is not None:
            continue
        sig = entry["signature"]
        tx = rpc(
            "getTransaction",
            [sig, {"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": 0,
                   "commitment": "finalized"}],
            _transport,
        ).get("result")
        if validate_transfer(tx, amount_base):
            return sig
    return None


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------
def mint_otp(conn) -> str:
    """4 digits, unique among live OTPs. Brute force is bounded by
    MAX_OTP_ATTEMPTS in claim(), not by the keyspace."""
    live = {r["otp"] for r in conn.execute(
        "SELECT otp FROM invoices WHERE status='PAID_UNCLAIMED' AND otp IS NOT NULL")}
    for _ in range(200):
        cand = f"{randbelow(10000):04d}"
        if cand not in live:
            return cand
    raise RuntimeError("OTP space exhausted — too many unclaimed invoices")


def cmd_invoice(item: str, channel: str, handle: str, reference: str,
                invoice_id: str = None) -> dict:
    """Price comes from ITEMS, never from the caller. There is no amount
    parameter, so no message can set one."""
    spec = ITEMS.get(item.strip().lower())
    if not spec:
        return {"error": f"unknown item {item!r}; stocked: {', '.join(ITEMS)}"}
    item = item.strip().lower()
    amount_base = spec["price_base"]
    with db() as conn:
        if invoice_id is None:
            # Atomic allocation: UPDATE...RETURNING is a single write txn, so two
            # concurrent invoices can never receive the same ID. Skip any ID that
            # already exists — the counter shares a namespace with IDs inserted
            # by hand (migrations, operator repairs), so it can start behind.
            for _ in range(50):
                n = conn.execute("UPDATE seq SET n = n + 1 RETURNING n").fetchone()["n"]
                invoice_id = f"INV-{n:04d}"
                if not conn.execute("SELECT 1 FROM invoices WHERE invoice_id=?",
                                    (invoice_id,)).fetchone():
                    break
            else:
                return {"error": "could not allocate an invoice id"}
        conn.execute(
            "INSERT INTO invoices (invoice_id, reference, item, amount_base, channel,"
            " handle, status, created_at) VALUES (?,?,?,?,?,?, 'AWAITING_PAYMENT', ?)",
            (invoice_id, reference, item, amount_base, channel, handle, now()),
        )
    return {"invoice_id": invoice_id, "status": "AWAITING_PAYMENT",
            "amount": f"{amount_base / 10 ** USDC_DECIMALS:.6f}".rstrip("0").rstrip(".")}


def cmd_watch(_transport=None) -> dict:
    """One cron pass. Returns ~200 tokens regardless of table size."""
    t = now()
    newly_paid, expired, errors = [], [], 0

    with db() as conn:
        # 1. Expiry sweep first — pure SQL, releases the slot lock.
        cur = conn.execute(
            "UPDATE invoices SET status='PAID_EXPIRED', otp=NULL"
            " WHERE status='PAID_UNCLAIMED' AND otp_expires_at <= ?"
            " RETURNING invoice_id", (t,))
        expired = [r["invoice_id"] for r in cur.fetchall()]

        open_invoices = conn.execute(
            "SELECT invoice_id, reference, amount_base, item, channel, handle"
            " FROM invoices WHERE status='AWAITING_PAYMENT' ORDER BY created_at"
        ).fetchall()

        # 2. Poll only open invoices. Zero open invoices == zero RPC calls.
        for inv in open_invoices:
            try:
                sig = check_reference(inv["reference"], inv["amount_base"], _transport)
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                errors += 1          # transient RPC failure: leave it open, retry next tick
                continue
            if not sig:
                continue
            otp = mint_otp(conn)
            try:
                conn.execute(
                    "UPDATE invoices SET status='PAID_UNCLAIMED', otp=?, signature=?,"
                    " paid_at=?, otp_expires_at=? WHERE invoice_id=?"
                    " AND status='AWAITING_PAYMENT'",
                    (otp, sig, t, t + OTP_TTL_SECS, inv["invoice_id"]),
                )
            except sqlite3.IntegrityError:
                errors += 1          # signature already settled another invoice: replay, skip
                continue
            newly_paid.append({
                "invoice_id": inv["invoice_id"],
                "item": inv["item"],
                "otp": otp,
                "channel": inv["channel"],
                "handle": inv["handle"],
                "expires_in_min": OTP_TTL_SECS // 60,
            })

        pending = conn.execute(
            "SELECT COUNT(*) c FROM invoices WHERE status='AWAITING_PAYMENT'"
        ).fetchone()["c"]

    return {"newly_paid": newly_paid, "expired": expired,
            "pending": pending, "rpc_errors": errors}


def cmd_claim(otp: str) -> dict:
    """Atomic single-use burn. Called by the serial daemon, never by the LLM.

    The UPDATE is the authorization decision: status, expiry and attempt budget
    are all in the WHERE clause, so a losing racer changes zero rows and gets no
    dispense. There is no window between 'check' and 'burn' for a second keypad
    entry to slip through.
    """
    t = now()
    with db() as conn:
        cur = conn.execute(
            "UPDATE invoices SET status='CLAIMED', claimed_at=?"
            " WHERE otp=? AND status='PAID_UNCLAIMED' AND otp_expires_at > ?"
            "   AND otp_attempts < ? RETURNING invoice_id, item",
            (t, otp, t, MAX_OTP_ATTEMPTS),
        )
        row = cur.fetchone()
        if row:
            slot = ITEMS.get(row["item"], {}).get("slot")
            if not slot:
                # Item was de-stocked after purchase. The OTP is already burned,
                # so this is an operator problem, not a retry: fail loud.
                return {"dispense": False, "reason": "no slot mapped for item",
                        "invoice_id": row["invoice_id"], "item": row["item"]}
            return {"dispense": True, "invoice_id": row["invoice_id"],
                    "item": row["item"], "slot": slot}
        # Wrong code: charge an attempt against every live invoice so a scanner
        # cannot walk the 10,000-code space for free.
        conn.execute(
            "UPDATE invoices SET otp_attempts = otp_attempts + 1"
            " WHERE status='PAID_UNCLAIMED'")
    return {"dispense": False, "reason": "invalid, expired, or already claimed"}


# --------------------------------------------------------------------------
# Refunds — T1. The agent builds an unsigned payment request; the operator's
# own wallet signs it. Nothing here can move funds.
# --------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


# Only these states may ever be refunded. CLAIMED means the drink was dispensed.
# AWAITING_PAYMENT means nothing was ever paid — refunding it mints free money.
REFUNDABLE = ("PAID_EXPIRED", "PAID_UNCLAIMED")


def resolve_payer(signature: str, _transport=None):
    """Refund destination, read from the chain — never from a chat message.

    This is the single most important control in the refund path. Every refund
    attack is an attempt to get an attacker-supplied address into this slot, so
    the address is derived from the settling transaction we already validated:
    the account whose USDC balance went DOWN by at least the invoice amount.
    A chat message cannot influence the result. If the transaction is ambiguous
    (several payers), we return None and the operator must resolve by hand.
    """
    tx = rpc("getTransaction",
             [signature, {"encoding": "jsonParsed",
                          "maxSupportedTransactionVersion": 0,
                          "commitment": "finalized"}],
             _transport).get("result")
    if not tx:
        return None
    meta = tx.get("meta") or {}

    def bal(entries):
        out = {}
        for e in entries or []:
            if e.get("mint") == USDC_MINT and e.get("owner"):
                out[e["owner"]] = int(e["uiTokenAmount"]["amount"])
        return out

    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    senders = [o for o in set(pre) | set(post)
               if o != MERCHANT and post.get(o, 0) - pre.get(o, 0) < 0]
    return senders[0] if len(senders) == 1 else None


def cmd_refund_request(invoice_id: str, reason: str = "", _transport=None) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT invoice_id, item, amount_base, status, signature FROM invoices"
            " WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not row:
            return {"error": "no such invoice"}
        if row["status"] not in REFUNDABLE:
            return {"error": f"not refundable from status {row['status']}"}
        if not row["signature"]:
            return {"error": "invoice has no settled payment"}

        payer = resolve_payer(row["signature"], _transport)
        if not payer:
            return {"error": "could not resolve a unique payer on-chain; operator must resolve manually"}

        conn.execute("UPDATE invoices SET status='EXPIRED_REFUND_REQUESTED'"
                     " WHERE invoice_id=? AND status IN (?,?)",
                     (invoice_id, *REFUNDABLE))
    return {"invoice_id": invoice_id, "item": row["item"],
            "amount": f"{row['amount_base'] / 10 ** USDC_DECIMALS:.2f}",
            "payer": payer, "signature": row["signature"], "reason": reason[:120],
            "status": "EXPIRED_REFUND_REQUESTED"}


def cmd_refund_approve(invoice_id: str, _transport=None) -> dict:
    """Called ONLY after the SOP checkpoint clears. Emits a Solana Pay URI the
    operator scans with their own wallet. We never hold a key."""
    with db() as conn:
        row = conn.execute(
            "SELECT item, amount_base, signature FROM invoices"
            " WHERE invoice_id=? AND status='EXPIRED_REFUND_REQUESTED'",
            (invoice_id,)).fetchone()
        if not row:
            return {"error": "invoice is not awaiting refund approval"}
        payer = resolve_payer(row["signature"], _transport)
        if not payer:
            return {"error": "payer no longer resolvable; aborting"}
        conn.execute("UPDATE invoices SET status='REFUND_APPROVED' WHERE invoice_id=?"
                     " AND status='EXPIRED_REFUND_REQUESTED'", (invoice_id,))

    amt = f"{row['amount_base'] / 10 ** USDC_DECIMALS:.6f}".rstrip("0").rstrip(".")
    uri = (f"solana:{payer}?amount={amt}&spl-token={USDC_MINT}"
           f"&reference={b58encode(os.urandom(32))}"
           f"&label=SolVend%20refund&message=Refund%20{invoice_id}")
    return {"invoice_id": invoice_id, "payer": payer, "amount": amt, "refund_uri": uri}


def cmd_refund_deny(invoice_id: str) -> dict:
    with db() as conn:
        cur = conn.execute(
            "UPDATE invoices SET status='REFUND_DENIED' WHERE invoice_id=?"
            " AND status='EXPIRED_REFUND_REQUESTED' RETURNING invoice_id", (invoice_id,))
        return {"invoice_id": invoice_id,
                "status": "REFUND_DENIED" if cur.fetchone() else "no-op"}


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "usage: solvend.py init-db|invoice|watch|claim"}))
        return 2
    cmd = args[0]
    if cmd == "init-db":
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with db() as conn:
            conn.executescript(SCHEMA)
        print(json.dumps({"ok": True, "db": DB_PATH}))
    elif cmd == "invoice":
        print(json.dumps(cmd_invoice(*args[1:7])))
    elif cmd == "watch":
        print(json.dumps(cmd_watch()))
    elif cmd == "claim":
        print(json.dumps(cmd_claim(args[1])))
    elif cmd == "refund-request":
        print(json.dumps(cmd_refund_request(args[1], args[2] if len(args) > 2 else "")))
    elif cmd == "refund-approve":
        print(json.dumps(cmd_refund_approve(args[1])))
    elif cmd == "refund-deny":
        print(json.dumps(cmd_refund_deny(args[1])))
    else:
        print(json.dumps({"error": f"unknown command {cmd!r}"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
