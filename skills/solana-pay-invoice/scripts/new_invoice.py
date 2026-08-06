#!/usr/bin/env python3
"""Mint a SolVend invoice: unique reference key + Solana Pay transfer-request URI.

Stdlib only. Records the invoice in the SolVend ledger so the payment-watcher
SOP can settle it — an invoice that never reaches the DB can never be paid.

The reference is 32 bytes from os.urandom, base58-encoded. It holds no funds and
is never signed with; it rides along as a read-only non-signer account so
`getSignaturesForAddress(reference)` can locate the settling transaction. Being
off-curve is fine and expected.

Usage:  new_invoice.py <item> <amount_usdc> <channel> <handle>
Prints one line of JSON on stdout.
"""

import json
import os
import sqlite3
import sys
from urllib.parse import quote

sys.path.insert(0, os.environ.get("SOLVEND_HOME", "/opt/solvend"))
import solvend  # noqa: E402

RECIPIENT = solvend.MERCHANT
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    # leading zero bytes are significant in base58
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


SESSIONS_DB = os.environ.get(
    "ZEROCLAW_SESSIONS_DB",
    os.path.expanduser("~/.zeroclaw/data/sessions/sessions.db"),
)


def resolve_caller():
    """-> (channel_id, room_id) for the customer currently talking, or None.

    ZeroClaw v0.8.4 script tools receive NOTHING — no argv, no stdin, and an
    environment with no caller context (verified by dumping it from inside a
    live tool call). `[[tools]]` also accepts no argument schema, so the agent
    cannot be given a parameter to pass the handle in.

    But ZeroClaw records the conversation itself. `session_metadata` carries
    `channel_id` ('telegram.shop') and `room_id` (the chat id) per session, so
    the tool looks the caller up instead of being told. Read-only, on a database
    this process never writes.

    This is *stronger* than a parameter would be: the customer never supplies
    the value, so no message can claim to be somebody else — the same control
    shape as the price and the refund destination.

    Ordering prefers a session with a live turn (`turn_id IS NOT NULL`) and
    falls back to most-recently-active. Two customers mid-turn on the same
    channel at the same instant could still race; for a single-slot machine
    serving one person at a time that is acceptable, and it is stated in the
    README rather than hidden.
    """
    channel_filter = os.environ.get("SOLVEND_CUSTOMER_CHANNEL")
    sql = ("SELECT channel_id, room_id FROM session_metadata"
           " WHERE channel_id IS NOT NULL AND room_id IS NOT NULL")
    params = []
    if channel_filter:
        sql += " AND channel_id = ?"
        params.append(channel_filter)
    sql += " ORDER BY (turn_id IS NOT NULL) DESC, last_activity DESC LIMIT 1"
    try:
        conn = sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (row[0], row[1]) if row else None


def main() -> int:
    args = sys.argv[1:]

    # No amount parameter, by design. The price is looked up from solvend.ITEMS,
    # so no message — however it is phrased, whoever it claims to be from —
    # can set what a customer is charged.
    if len(args) == 3:
        item, channel, handle = args          # explicit: CLI tests, operator use
    elif len(args) == 1:
        item = args[0]                        # agent path: resolve the caller
        caller = resolve_caller()
        if caller is None:
            print(json.dumps({"error": "no active session found; cannot address "
                                       "the customer. Retry, or pass "
                                       "<item> <channel> <handle> explicitly."}))
            return 2
        channel, handle = caller
    else:
        print(json.dumps({"error": "usage: new_invoice.py <item> "
                                   "[<channel> <handle>]"}))
        return 2

    reference = b58encode(os.urandom(32))
    rec = solvend.cmd_invoice(item, channel, handle, reference)
    if "error" in rec:
        print(json.dumps(rec))
        return 2
    invoice_id, amt = rec["invoice_id"], rec["amount"]

    uri = (
        f"solana:{RECIPIENT}"
        f"?amount={amt}"
        f"&spl-token={solvend.USDC_MINT}"
        f"&reference={reference}"
        f"&label={quote('SolVend')}"
        f"&message={quote(f'Invoice {invoice_id} - {item}')}"
    )

    print(json.dumps({
        "invoice_id": invoice_id,
        "reference": reference,
        "uri": uri,
        "amount": amt,
        "item": item,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
