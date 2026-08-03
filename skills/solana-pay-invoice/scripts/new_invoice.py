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


def main() -> int:
    if len(sys.argv) != 4:
        print(json.dumps({"error": "usage: new_invoice.py <item> <channel> <handle>"}))
        return 2

    # No amount parameter, by design. The price is looked up from solvend.ITEMS,
    # so no message — however it is phrased, whoever it claims to be from —
    # can set what a customer is charged.
    item, channel, handle = sys.argv[1:4]

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
