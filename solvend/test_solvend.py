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


def fresh(ref="REF1", item="cola"):
    with solvend.db() as c:
        c.executescript(solvend.SCHEMA)
        c.execute("DELETE FROM invoices")
    return solvend.cmd_invoice(item, "whatsapp.shop", "+5511999", ref, "INV-0001")


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

PAYER = "PAYER11111111111111111111111111111111111111"


def tx2(merchant_post=1_500_000, payer_pre=10_000_000):
    """Realistic two-party transfer: payer down, merchant up."""
    return {"meta": {"err": None,
            "preTokenBalances": [
                {"mint": solvend.USDC_MINT, "owner": MERCHANT,
                 "uiTokenAmount": {"amount": "0"}},
                {"mint": solvend.USDC_MINT, "owner": PAYER,
                 "uiTokenAmount": {"amount": str(payer_pre)}}],
            "postTokenBalances": [
                {"mint": solvend.USDC_MINT, "owner": MERCHANT,
                 "uiTokenAmount": {"amount": str(merchant_post)}},
                {"mint": solvend.USDC_MINT, "owner": PAYER,
                 "uiTokenAmount": {"amount": str(payer_pre - merchant_post)}}]}}


def paid_and_expired():
    """An invoice that was paid, went unclaimed, and expired."""
    fresh()
    t = transport_for(tx2())
    r = solvend.cmd_watch(t)
    otp = r["newly_paid"][0]["otp"]
    with solvend.db() as c:
        c.execute("UPDATE invoices SET otp_expires_at=? WHERE otp=?",
                  (int(time.time()) - 1, otp))
    solvend.cmd_watch(t)
    return t


print("\nrefund — destination is resolved on-chain, never from chat")
t = paid_and_expired()
check("payer resolved from transaction", solvend.resolve_payer("SIG1", t) == PAYER)
r = solvend.cmd_refund_request("INV-0001", "machine jammed", t)
check("refund opens with on-chain payer", r.get("payer") == PAYER)
check("refund amount comes from ledger", r.get("amount") == "1.50")
check("cmd_refund_request has no destination parameter",
      "address" not in solvend.cmd_refund_request.__code__.co_varnames
      and "destination" not in solvend.cmd_refund_request.__code__.co_varnames)
r = solvend.cmd_refund_approve("INV-0001", t)
check("approved refund URI targets the on-chain payer", PAYER in r.get("refund_uri", ""))
check("attacker address absent from refund URI", ATTACKER not in r.get("refund_uri", ""))

print("\nrefund — state guards")
t = paid_and_expired()
solvend.cmd_refund_request("INV-0001", "", t)
solvend.cmd_refund_approve("INV-0001", t)
check("double refund refused",
      "error" in solvend.cmd_refund_approve("INV-0001", t))

fresh()                                    # AWAITING_PAYMENT: never paid
check("refunding an unpaid invoice refused",
      "error" in solvend.cmd_refund_request("INV-0001", "", transport_for(tx2())))

fresh()                                    # CLAIMED: drink was dispensed
t = transport_for(tx2())
otp = solvend.cmd_watch(t)["newly_paid"][0]["otp"]
solvend.cmd_claim(otp)
check("refunding a dispensed drink refused",
      "error" in solvend.cmd_refund_request("INV-0001", "", t))

t = paid_and_expired()
solvend.cmd_refund_request("INV-0001", "", t)
solvend.cmd_refund_deny("INV-0001")
check("approve after deny refused",
      "error" in solvend.cmd_refund_approve("INV-0001", t))

print("\ncatalogue — price is not a parameter")
check("cmd_invoice has no amount parameter",
      "amount_disp" not in solvend.cmd_invoice.__code__.co_varnames
      and "amount" not in solvend.cmd_invoice.__code__.co_varnames)
r = fresh(item="cola")
check("cola priced from ITEMS, not caller", r["amount"] == "1.5")
with solvend.db() as c:
    check("ledger stores 1_500_000 base units",
          c.execute("SELECT amount_base FROM invoices").fetchone()[0] == 1_500_000)
check("unknown item refused",
      "error" in solvend.cmd_invoice("beer", "whatsapp.shop", "+55", "REFX", "INV-9001"))
check("item casing normalised",
      "error" not in solvend.cmd_invoice(" CoLa ", "whatsapp.shop", "+55", "REFY", "INV-9002"))
check("auto-allocated IDs never collide",
      len({solvend.cmd_invoice("water", "c", "h", f"REFZ{i}")["invoice_id"]
           for i in range(25)}) == 25)

print("\nkeypad — slot resolution for the serial daemon")
fresh(item="energy")
t = transport_for(tx2(merchant_post=2_500_000))
r = solvend.cmd_watch(t)
check("energy settles at its own price", len(r["newly_paid"]) == 1)
claim = solvend.cmd_claim(r["newly_paid"][0]["otp"])
check("claim returns the gantry slot", claim.get("slot") == "drink-3")
check("denied claim carries no slot", "slot" not in solvend.cmd_claim("0000"))

fresh(item="cola")
t = transport_for(tx2(merchant_post=1_000_000))     # paid water price for a cola
r = solvend.cmd_watch(t)
check("underpaying a pricier item does not settle", r["newly_paid"] == [])

print("\nrefund — ambiguity fails closed")
amb = {"meta": {"err": None, "preTokenBalances": [
        {"mint": solvend.USDC_MINT, "owner": PAYER, "uiTokenAmount": {"amount": "10"}},
        {"mint": solvend.USDC_MINT, "owner": ATTACKER, "uiTokenAmount": {"amount": "10"}}],
       "postTokenBalances": [
        {"mint": solvend.USDC_MINT, "owner": PAYER, "uiTokenAmount": {"amount": "5"}},
        {"mint": solvend.USDC_MINT, "owner": ATTACKER, "uiTokenAmount": {"amount": "5"}}]}}
check("two candidate payers resolves to None",
      solvend.resolve_payer("SIG1", transport_for(amb)) is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
