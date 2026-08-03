#!/bin/sh
# Env wrapper: ZeroClaw script tools are assumed NOT to inherit host env.
# Install: /opt/solvend/bin/solvend-run.sh  (chmod 750, root:zeroclaw)
# Usage:   solvend-run.sh watch | claim 4821 | refund-request INV-0412 "jam"
set -eu
. /etc/solvend/env          # chmod 600 root:zeroclaw — never world-readable
exec python3 /opt/solvend/solvend.py "$@"
