#!/usr/bin/env bash
# SolVend Phase 6 — install the code and its secret onto the Pi.
# Run from the repo root on the Pi (it locates the repo from its own path):
#
#   bash deploy/pi-deploy.sh \
#     --rpc-url 'https://mainnet.helius-rpc.com/?api-key=…' \
#     --recipient <merchant pubkey>
#
#   # devnet rehearsal on the Pi: add the mint override
#   bash deploy/pi-deploy.sh --rpc-url https://api.devnet.solana.com \
#     --recipient <pubkey> --usdc-mint 4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU
#
# Re-runnable: code is re-copied every time, but /etc/solvend/env is never
# overwritten without --force, so a redeploy cannot silently drop your RPC key.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ME="${USER:-$(id -un)}"

RPC_URL=""; RECIPIENT=""; USDC_MINT=""; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rpc-url)   RPC_URL="$2";   shift 2 ;;
    --recipient) RECIPIENT="$2"; shift 2 ;;
    --usdc-mint) USDC_MINT="$2"; shift 2 ;;
    --force)     FORCE=1;        shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

# ---------------------------------------------------------------- code
echo "==> Installing code from $REPO"
sudo mkdir -p /opt/solvend /var/lib/solvend /etc/solvend
sudo cp -r "$REPO/solvend/." /opt/solvend/
sudo rm -rf /opt/solvend/__pycache__ /opt/solvend/skills
sudo cp -r "$REPO/skills" /opt/solvend/skills

# The ledger is written by both the agent and the serial daemon, so it must be
# owned by the login user. /opt/solvend stays root-owned and read-only to them.
sudo chown -R "$ME:$ME" /var/lib/solvend
# `sudo cp` leaves these root:root. chmod 750 on root:root grants execute to root
# and the root group only, so the login user — who actually runs them, via the
# agent and via cron — gets "Permission denied". Group them to the login user.
sudo chown root:"$ME" /opt/solvend/bin/* 2>/dev/null || true
sudo chmod 750 /opt/solvend/bin/* 2>/dev/null || true
echo "  code -> /opt/solvend, ledger dir -> /var/lib/solvend"

# ---------------------------------------------------------------- secret
if [ -f /etc/solvend/env ] && [ "$FORCE" -eq 0 ]; then
  echo "==> /etc/solvend/env already exists — left untouched (--force to rewrite)"
else
  [ -n "$RPC_URL" ]   || die "--rpc-url is required to create /etc/solvend/env"
  [ -n "$RECIPIENT" ] || die "--recipient is required to create /etc/solvend/env"
  # Guard against shipping the runbook's placeholders into production.
  case "$RECIPIENT" in
    *YourMerchantPubkey*|*MERCHANT_WALLET_PUBKEY_HERE*) die "--recipient is still a placeholder" ;;
  esac
  case "$RPC_URL" in
    *YOUR_KEY*) die "--rpc-url still contains YOUR_KEY" ;;
  esac

  echo "==> Writing /etc/solvend/env"
  # Written via a root-only umask so the key is never briefly world-readable,
  # and never echoed to the terminal or the shell history of this script.
  ( umask 077
    {
      echo "SOLVEND_RPC_URL=$RPC_URL"
      echo "SOLVEND_RECIPIENT=$RECIPIENT"
      echo "SOLVEND_DB=/var/lib/solvend/solvend.db"
      echo "SOLVEND_HOME=/opt/solvend"
      [ -n "$USDC_MINT" ] && echo "SOLVEND_USDC_MINT=$USDC_MINT"
      echo "SOLVEND_SERIAL_PORT=${SOLVEND_SERIAL_PORT:-/dev/ttyUSB0}"
    } | sudo tee /etc/solvend/env >/dev/null
  )
  # 640, not 600. The wrappers source this file as the login user, and mode 600
  # gives the group nothing — root:pi 600 is readable by root alone, so every
  # wrapper fails on a file that looks correctly owned. 640 lets the pi group
  # read it and still denies everyone else.
  sudo chown "root:$ME" /etc/solvend/env
  sudo chmod 640 /etc/solvend/env
  echo "  written, chmod 640, root:$ME"
fi

# ---------------------------------------------------------------- ledger
echo "==> Initialising the ledger"
/opt/solvend/bin/solvend-run.sh init-db

# ---------------------------------------------------------------- verify
echo
echo "==> Verification"
fail=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "  ok    $1"; else echo "  FAIL  $1"; fail=$((fail+1)); fi; }

chk "/opt/solvend/solvend.py present"        "[ -f /opt/solvend/solvend.py ]"
chk "/opt/solvend/skills present"            "[ -d /opt/solvend/skills ]"
chk "env file is chmod 640"                  "[ \"\$(stat -c %a /etc/solvend/env)\" = 640 ]"
chk "env file readable by $ME"               "[ -r /etc/solvend/env ]"
chk "wrappers executable by $ME"             "[ -x /opt/solvend/bin/solvend-run.sh ]"
chk "env file owned root:$ME"                "[ \"\$(stat -c %U:%G /etc/solvend/env)\" = \"root:$ME\" ]"
chk "ledger created"                         "[ -f /var/lib/solvend/solvend.db ]"
chk "ledger writable by $ME"                 "[ -w /var/lib/solvend/solvend.db ]"
# A live RPC round-trip through the real wrapper: proves the env file parses,
# the key works, and the merchant address is accepted, before any customer pays.
chk "solvend-run.sh watch succeeds"          "/opt/solvend/bin/solvend-run.sh watch"

echo
if [ "$fail" -gt 0 ]; then
  echo "$fail check(s) failed — do not continue to Phase 7."
  exit 1
fi
echo "Phase 6 deploy OK. Secrets stayed in /etc/solvend/env; nothing was printed."
