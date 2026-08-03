#!/usr/bin/env python3
"""Host-run tests for the SolVend core. Mocked RPC — no live network.

Run:  python test_solvend.py
"""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["SOLVEND_DB"] = os.path.join(TMP, "test.db")
os.environ["SOLVEND_RECIPIENT"] = "MERCHANT111111111111111111111111111111111111"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import solvend  # noqa: E402

MERCHANT = os.environ["SOLVEND_RECIPIENT"]
ATTACKER = "ATTACKER1111111111111111111111111111111111111"
FAKE_MINT = "FAKEMINT111111111111111111111111111111111111"

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def tx(owner=MERCHANT, mint=solvend.USDC_MINT, pre=0, post=1_500_000, err=None):
    return {"meta": {"err": err,
                     "preTokenBalances": [{"mint": mint, "owner": owner,
                                           "uiTokenAmount": {"amount": str(pre)}}],
                     "postTokenBalances": [{"mint": mint, "owner": owner,
                                            "uiTokenAmount": {"amount": str(post)}}]}}


def transport_for(tx_obj, sig="SIG1"):
    def _t(method, params):
        if method == "getSignaturesForAddress":
            return {"result": [{"signature": sig, "err": None}]}
        return {"result": tx_obj}
    return _t


def fresh(ref="REF1", amount="1.50", item="cola"):
    with solvend.db() as c:
        c.executescript(solvend.SCHEMA)
        c.execute("DELETE FROM invoices")
    return solvend.cmd_invoice(item, amount, "whatsapp.shop", "+5511999", ref, "INV-0001")


print("\nvalidate_transfer — the anti-spoof boundary")
fresh()
check("exact payment accepted", solvend.validate_transfer(tx(post=1_500_000), 1_500_000))
check("overpayment accepted", solvend.validate_transfer(tx(post=2_000_000), 1_500_000))
check("UNDERPAYMENT rejected", not solvend.validate_transfer(tx(post=1), 1_500_000))
check("wrong recipient rejected",
      not solvend.validate_transfer(tx(owner=ATTACKER), 1_500_000))
check("wrong mint rejected", not solvend.validate_transfer(tx(mint=FAKE_MINT), 1_500_000))
check("failed tx rejected", not solvend.validate_transfer(tx(err={"e": 1}), 1_500_000))
check("empty tx rejected", not solvend.validate_transfer(None, 1_500_000))
check("pre-existing balance not double-counted",
      not solvend.validate_transfer(tx(pre=1_500_000, post=1_500_000), 1_500_000))

print("\nwatch — settlement")
fresh()
r = solvend.cmd_watch(transport_for(tx(post=1_500_000)))
check("valid payment settles", len(r["newly_paid"]) == 1)
otp = r["newly_paid"][0]["otp"] if r["newly_paid"] else None
check("OTP is 4 digits", bool(otp) and len(otp) == 4 and otp.isdigit())
check("response is compact", len(json.dumps(r)) < 400)

print("\nwatch — attacker attaches reference to a junk transaction")
fresh()
r = solvend.cmd_watch(transport_for(tx(post=1)))          # 0.000001 USDC
check("dust payment does NOT settle", r["newly_paid"] == [])
r = solvend.cmd_watch(transport_for(tx(owner=ATTACKER, post=1_500_000)))
check("payment to attacker does NOT settle", r["newly_paid"] == [])

print("\nclaim — single-use burn")
fresh()
r = solvend.cmd_watch(transport_for(tx(post=1_500_000)))
otp = r["newly_paid"][0]["otp"]
check("correct OTP dispenses", solvend.cmd_claim(otp)["dispense"] is True)
check("REPLAY of same OTP refused", solvend.cmd_claim(otp)["dispense"] is False)
check("wrong OTP refused", solvend.cmd_claim("0000")["dispense"] is False)

print("\nclaim — brute-force budget")
fresh()
r = solvend.cmd_watch(transport_for(tx(post=1_500_000)))
otp = r["newly_paid"][0]["otp"]
for _ in range(solvend.MAX_OTP_ATTEMPTS):
    solvend.cmd_claim("9999" if otp != "9999" else "1111")
check("correct OTP dead after attempt budget", solvend.cmd_claim(otp)["dispense"] is False)

print("\nexpiry sweep")
fresh()
r = solvend.cmd_watch(transport_for(tx(post=1_500_000)))
otp = r["newly_paid"][0]["otp"]
with solvend.db() as c:
    c.execute("UPDATE invoices SET otp_expires_at=? WHERE otp=?", (int(time.time()) - 1, otp))
r = solvend.cmd_watch(transport_for(tx()))
check("expired invoice transitions to PAID_EXPIRED", r["expired"] == ["INV-0001"])
check("expired OTP no longer dispenses", solvend.cmd_claim(otp)["dispense"] is False)

print("\nrpc failure")
fresh()


def boom(method, params):
    raise OSError("rpc down")


r = solvend.cmd_watch(boom)
check("RPC outage fails closed, invoice stays open",
      r["newly_paid"] == [] and r["pending"] == 1 and r["rpc_errors"] == 1)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
