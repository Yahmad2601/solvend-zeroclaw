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

    # Write the PNG first: the terminal render below is the fragile step, and
    # losing the scannable file to a console encoding error would be the worst
    # possible failure right when you are trying to pay an invoice.
    if len(sys.argv) > 2:
        qr.make_image().save(sys.argv[2])
        print(f"saved {sys.argv[2]}\n")

    # print_ascii draws with U+2588 block characters. A Windows console on the
    # legacy cp1252 code page cannot encode those and raises UnicodeEncodeError,
    # so ask for UTF-8 first and degrade to a PNG rather than dying.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        qr.print_ascii(invert=True)     # invert = scannable on a dark terminal
    except UnicodeEncodeError:
        print("terminal cannot render block characters — "
              "re-run with a PNG path:\n"
              f'  python tools/make_qr.py "{uri[:40]}…" invoice.png')
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
