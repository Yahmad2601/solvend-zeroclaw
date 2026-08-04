#!/usr/bin/env python3
"""Render a Solana Pay URI as a scannable QR — terminal and PNG.

Testing aid only. In the live flow the agent sends the URI as text over
WhatsApp; this exists so you can scan an invoice from your laptop screen with
Phantom during the Phase 3 rehearsal.

    pip install qrcode
    python tools/make_qr.py "solana:...."          # prints in terminal
    python tools/make_qr.py "solana:...." out.png  # also writes a PNG
"""
import sys

try:
    import qrcode
except ImportError:
    sys.exit("pip install qrcode")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    uri = sys.argv[1]
    if not uri.startswith("solana:"):
        print(f"warning: does not look like a Solana Pay URI: {uri[:40]}…\n")

    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)     # invert = scannable on a dark terminal

    if len(sys.argv) > 2:
        qr.make_image().save(sys.argv[2])
        print(f"\nsaved {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
