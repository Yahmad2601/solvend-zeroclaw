#!/bin/sh
# Env wrapper: ZeroClaw script tools are assumed NOT to inherit host env.
# Install: /opt/solvend/bin/solvend-run.sh  (chmod 750, root:zeroclaw)
# Usage:   solvend-run.sh watch | claim 4821 | refund-request INV-0412 "jam"
set -eu
# `set -a` is load-bearing. Plain `. file` on KEY=value lines creates *shell*
# variables, which exec does not pass on — solvend.py would silently fall back
# to its defaults (mainnet RPC, MERCHANT_WALLET_PUBKEY_HERE) with a correct-looking
# env file sitting right there. allexport marks them for export instead. The file
# format stays plain KEY=value, so systemd EnvironmentFile= still reads it.
set -a
. /etc/solvend/env          # chmod 600 root:zeroclaw — never world-readable
set +a
exec python3 /opt/solvend/solvend.py "$@"
