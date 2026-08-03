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

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
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


def cmd_invoice(item: str, amount_disp: str, channel: str, handle: str, reference: str,
                invoice_id: str = None) -> dict:
    amount_base = int(Decimal(amount_disp) * (10 ** USDC_DECIMALS))
    if amount_base <= 0:
        return {"error": "amount must be positive"}
    with db() as conn:
        if invoice_id is None:
            # Atomic allocation: UPDATE...RETURNING is a single write txn, so two
            # concurrent invoices can never receive the same ID.
            n = conn.execute("UPDATE seq SET n = n + 1 RETURNING n").fetchone()["n"]
            invoice_id = f"INV-{n:04d}"
        conn.execute(
            "INSERT INTO invoices (invoice_id, reference, item, amount_base, channel,"
            " handle, status, created_at) VALUES (?,?,?,?,?,?, 'AWAITING_PAYMENT', ?)",
            (invoice_id, reference, item, amount_base, channel, handle, now()),
        )
    return {"invoice_id": invoice_id, "status": "AWAITING_PAYMENT"}


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
            return {"dispense": True, "invoice_id": row["invoice_id"], "item": row["item"]}
        # Wrong code: charge an attempt against every live invoice so a scanner
        # cannot walk the 10,000-code space for free.
        conn.execute(
            "UPDATE invoices SET otp_attempts = otp_attempts + 1"
            " WHERE status='PAID_UNCLAIMED'")
    return {"dispense": False, "reason": "invalid, expired, or already claimed"}


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
    else:
        print(json.dumps({"error": f"unknown command {cmd!r}"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
