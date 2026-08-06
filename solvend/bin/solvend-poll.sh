#!/bin/sh
# Minute poller. Runs as a [cron.solvend_poll] SHELL job — no model in the loop.
#
# Cost: 1440 runs/day at zero tokens. The agent is woken only when something
# actually needs judgment. On a quiet night this script costs nothing.
#
# Division of labour, deliberate:
#   OTP delivery  -> `zeroclaw channel send`, deterministic. The code is minted
#                    by SQL and copied verbatim to the customer. No model can
#                    hallucinate a digit, leak another customer's code, or be
#                    talked into issuing one.
#   Expiry/refund -> `zeroclaw agent -m`, because those are conversations that
#                    genuinely need judgment.
#
# Install: /opt/solvend/bin/solvend-poll.sh (chmod 750, root:zeroclaw)
set -eu
# set -a: see solvend-run.sh. Without it `watch` below runs against the default
# mainnet RPC and a placeholder merchant, so it would never see a payment.
set -a
. /etc/solvend/env
set +a

# Resolve zeroclaw by absolute path. It installs to ~/.cargo/bin, which a
# scheduler-launched job does NOT get on PATH — observed 2026-08-06: the poller
# settled a real payment, minted the OTP, then failed to deliver it because a
# bare `zeroclaw` was "command not found". The invoice sits PAID_UNCLAIMED and
# the customer is charged with no code, which is the worst failure this system
# has. Override with ZEROCLAW_BIN in /etc/solvend/env if it lives elsewhere.
ZEROCLAW="${ZEROCLAW_BIN:-}"
[ -n "$ZEROCLAW" ] || ZEROCLAW=$(command -v zeroclaw 2>/dev/null || true)
for _cand in "${HOME:-/home/pi}/.cargo/bin/zeroclaw" /home/pi/.cargo/bin/zeroclaw \
             /usr/local/bin/zeroclaw /usr/bin/zeroclaw; do
    [ -n "$ZEROCLAW" ] && break
    [ -x "$_cand" ] && ZEROCLAW="$_cand"
done
if [ -z "$ZEROCLAW" ] || [ ! -x "$ZEROCLAW" ]; then
    # Fail before settling anything: a payment detected but undeliverable is
    # worse than one detected a minute late.
    logger -t solvend "FATAL: zeroclaw binary not found — refusing to settle"
    exit 0
fi

OUT=$(python3 /opt/solvend/solvend.py watch 2>/dev/null) || exit 0
[ -n "$OUT" ] || exit 0

# Deterministic OTP delivery. TAB-separated so a crafted item name cannot
# smuggle a field break; item names come from the skill's fixed price list.
printf '%s' "$OUT" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for p in d.get("newly_paid",[]):
    msg=("Payment confirmed for %s. Your code is %s. Enter it on the keypad "
         "within %d minutes. It works once." % (p["item"],p["otp"],p["expires_in_min"]))
    print("%s\t%s\t%s" % (p["channel"],p["handle"],msg))
' | while IFS="$(printf '\t')" read -r chan recip msg; do
    "$ZEROCLAW" channel send "$msg" --channel-id "$chan" --recipient "$recip" \
      || logger -t solvend "OTP delivery FAILED for $recip"   # [?] verify --channel-id accepts an alias
done

# Wake the agent only for things needing judgment.
EXPIRED=$(printf '%s' "$OUT" | python3 -c '
import json,sys; d=json.load(sys.stdin); print(",".join(d.get("expired",[])))')
ERRS=$(printf '%s' "$OUT" | python3 -c '
import json,sys; print(json.load(sys.stdin).get("rpc_errors",0))')

if [ -n "$EXPIRED" ]; then
    "$ZEROCLAW" agent -a solvend -m "SYSTEM_EVENT expired_invoices=$EXPIRED. Post one operator line per invoice noting the slot lock is released. Do not message customers."
fi
[ "$ERRS" -gt 0 ] && logger -t solvend "rpc_errors=$ERRS"
exit 0
