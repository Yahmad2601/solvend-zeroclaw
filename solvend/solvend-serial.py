#!/usr/bin/env python3
"""SolVend serial daemon — the keypad half of the machine.

Runs on the Pi as a systemd service. Reads KEYPAD: lines from the ESP32,
resolves them against the ledger with the atomic burn, and answers with a
DISPENSE or DENY command.

No LLM. No network. No agent. This process cannot be prompt-injected because
nothing it reads is ever shown to a model: the ESP32 speaks a four-token
protocol, and the only input that reaches SQL is four digits validated by
regex. A customer standing at the keypad has strictly less authority than one
sending a WhatsApp message.

Requires pyserial:  sudo apt install python3-serial

Install: /opt/solvend/solvend-serial.py, run by solvend-serial.service with
EnvironmentFile=/etc/solvend/env.
"""

import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.environ.get("SOLVEND_HOME", "/opt/solvend"))
import solvend  # noqa: E402

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial missing: sudo apt install python3-serial")

PORT = os.environ.get("SOLVEND_SERIAL_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("SOLVEND_SERIAL_BAUD", "115200"))
RECONNECT_SECS = 3

# Strict: exactly four digits, nothing else. Line noise on a USB cable is real,
# and a garbled line must never reach the database.
KEYPAD_RE = re.compile(r"^KEYPAD:(\d{4})$")

log = logging.getLogger("solvend-serial")


def handle_keypad(ser, otp: str) -> None:
    """One keypress -> one atomic decision -> one command back."""
    try:
        result = solvend.cmd_claim(otp)
    except Exception:
        # A DB error must never dispense. Deny, log, stay up.
        log.exception("claim failed for a keypad entry")
        send(ser, "DENY:System error")
        return

    if result.get("dispense"):
        slot = result["slot"]
        log.info("DISPENSE %s invoice=%s item=%s",
                 slot, result["invoice_id"], result["item"])
        send(ser, f"DISPENSE:{slot}")
    else:
        # Deliberately uninformative to the customer: distinguishing "wrong
        # code" from "expired" from "already used" would let someone at the
        # keypad probe which codes exist. The operator log has the detail.
        log.info("DENY reason=%s", result.get("reason"))
        send(ser, "DENY:Not valid")


def send(ser, line: str) -> None:
    ser.write((line + "\n").encode("ascii", "ignore"))
    ser.flush()


def handle_line(ser, line: str) -> None:
    m = KEYPAD_RE.match(line)
    if m:
        handle_keypad(ser, m.group(1))
    elif line == "EVENT:BOOT":
        log.warning("ESP32 booted/reset")
    elif line.startswith("EVENT:DISPENSED:"):
        # Physical confirmation the gantry finished. The OTP was already burned
        # before this arrives — deliberately. If the machine jams mid-cycle we
        # would rather owe one customer a refund (an operator-approved,
        # on-chain-destined refund) than leave a live code that dispenses twice.
        log.info("gantry completed %s", line.rsplit(":", 1)[-1])
    elif line.startswith("EVENT:ERROR:"):
        log.error("firmware reported %s", line.rsplit(":", 1)[-1])
    elif line:
        log.debug("ignored: %r", line[:80])


def run_once() -> None:
    """One connected session. Returns on disconnect so main() can retry."""
    with serial.Serial(PORT, BAUD, timeout=1) as ser:
        log.info("connected %s @ %d", PORT, BAUD)
        time.sleep(2)            # ESP32 auto-resets on DTR; wait it out
        ser.reset_input_buffer()
        while True:
            raw = ser.readline()
            if not raw:
                continue         # timeout tick, keep waiting
            try:
                line = raw.decode("ascii", "ignore").strip()
            except Exception:
                continue
            handle_line(ser, line)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("SOLVEND_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not os.path.exists(solvend.DB_PATH):
        log.error("ledger missing at %s — run `solvend.py init-db`", solvend.DB_PATH)
        return 1

    # Never exit on a cable yank or an ESP32 reflash; a vending machine that
    # stops accepting codes because someone bumped the USB plug is a dead shop.
    while True:
        try:
            run_once()
        except serial.SerialException as e:
            log.warning("serial down (%s); retrying in %ds", e, RECONNECT_SECS)
        except KeyboardInterrupt:
            log.info("shutting down")
            return 0
        except Exception:
            log.exception("unexpected error; retrying in %ds", RECONNECT_SECS)
        time.sleep(RECONNECT_SECS)


if __name__ == "__main__":
    sys.exit(main())
