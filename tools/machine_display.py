#!/usr/bin/env python3
"""The machine's screen: a QR for the invoice currently awaiting payment.

Solana Pay is a point-of-sale spec and QR is its primary transport. A `solana:`
URI pasted into a chat is a deep link, not a URL — Telegram will not linkify it,
and neither Solflare nor Phantom offers a reliable "paste a payment URI" flow.
So the customer scans the machine, exactly as they would at any card terminal.
The chat channel carries the conversation and the dispense code; it is not the
payment rail.

Run it on the Pi with a monitor attached and put a browser on it fullscreen:

    set -a; . /etc/solvend/env; set +a
    python3 tools/machine_display.py                 # then open localhost:8080

Read-only. It opens the ledger in SQLite read-only mode, mints nothing, settles
nothing, and holds no key — it only renders what `new_invoice.py` already wrote.
The URI is rebuilt from the ledger row plus the same environment the invoice
tool used, so the amount and mint cannot drift from what was actually charged.

    pip install qrcode
"""
import base64
import io
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

try:
    import qrcode
except ImportError:
    sys.exit("pip install qrcode")

DB = os.environ.get("SOLVEND_DB", "/var/lib/solvend/solvend.db")
RECIPIENT = os.environ.get("SOLVEND_RECIPIENT", "MERCHANT_WALLET_PUBKEY_HERE")
MINT = os.environ.get("SOLVEND_USDC_MINT",
                      "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
PORT = int(os.environ.get("SOLVEND_DISPLAY_PORT", "8080"))
USDC_DECIMALS = 6


def current_invoice():
    """Newest AWAITING_PAYMENT row, or None. Read-only; never writes."""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT invoice_id, item, amount_base, reference FROM invoices"
                " WHERE status='AWAITING_PAYMENT' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    invoice_id, item, amount_base, reference = row
    amount = f"{amount_base / 10 ** USDC_DECIMALS:.6f}".rstrip("0").rstrip(".")
    uri = (f"solana:{RECIPIENT}?amount={amount}&spl-token={MINT}"
           f"&reference={reference}&label={quote('SolVend')}"
           f"&message={quote(f'Invoice {invoice_id} - {item}')}")
    return {"invoice_id": invoice_id, "item": item, "amount": amount, "uri": uri}


def qr_data_uri(uri: str) -> str:
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(uri)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>SolVend</title><style>
 body{{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;
      height:100vh;display:flex;flex-direction:column;align-items:center;
      justify-content:center;text-align:center}}
 img{{width:min(60vh,60vw);image-rendering:pixelated;background:#fff;padding:16px;
      border-radius:12px}}
 .item{{font-size:6vh;font-weight:600;margin:3vh 0 0}}
 .amt{{font-size:4vh;color:#7fd67f;margin:.5vh 0 3vh}}
 .idle{{font-size:5vh;color:#888}}
 .inv{{font-size:2vh;color:#666;margin-top:2vh;letter-spacing:.1em}}
</style></head><body>{body}</body></html>"""

IDLE = ('<div class="idle">SolVend</div>'
        '<div class="inv">message the shop bot to order</div>')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        inv = current_invoice()
        if inv:
            body = (f'<img src="{qr_data_uri(inv["uri"])}" alt="Solana Pay QR">'
                    f'<div class="item">{inv["item"]}</div>'
                    f'<div class="amt">{inv["amount"]} USDC</div>'
                    f'<div class="inv">{inv["invoice_id"]} &middot; scan to pay</div>')
        else:
            body = IDLE
        payload = PAGE.format(body=body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):        # keep the machine's console quiet
        pass


if __name__ == "__main__":
    print(f"SolVend display on http://0.0.0.0:{PORT}  (ledger: {DB})")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
