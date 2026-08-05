#!/usr/bin/env bash
# SolVend Phase 4 — Raspberry Pi base. Run once on a freshly flashed Pi:
#
#   bash pi-bootstrap.sh              # install + configure
#   bash pi-bootstrap.sh --check      # verify only, change nothing
#
# Idempotent: safe to re-run after the reboot it asks for.
#
# This exists because two Phase 4 steps fail *silently* and surface much later
# as something that looks like a bug in SolVend:
#   * an unsynchronised clock makes every OTP expiry wrong — codes die instantly
#     or never expire, and the Pi has no real-time clock to fall back on
#   * missing `dialout` membership breaks the serial bridge in Phase 7 with a
#     permission error three phases away from its cause
# So this script verifies rather than assumes, and exits non-zero if the box is
# not actually ready.
set -uo pipefail

# Derive the login name rather than trust $USER: it is unset under `sudo bash`,
# cron and some non-login shells, which would abort the script under `set -u`.
ME="${USER:-$(id -un)}"

TZ_WANT="${SOLVEND_TZ:-America/Sao_Paulo}"
PKGS=(python3-serial git qrencode socat)
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

pass=0; fail=0; warn=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  warn  %s\n' "$1"; warn=$((warn+1)); }

if [ "$CHECK_ONLY" -eq 0 ]; then
  echo "==> Packages"
  sudo apt-get update -qq
  # No -y upgrade: this box is about to hold a payment ledger. Install only.
  sudo apt-get install -y "${PKGS[@]}"

  echo "==> Timezone + clock"
  sudo timedatectl set-timezone "$TZ_WANT"
  sudo timedatectl set-ntp true || note "could not force NTP on; check manually"

  echo "==> Serial group"
  if id -nG "$ME" | grep -qw dialout; then
    echo "  already in dialout"
  else
    sudo usermod -aG dialout "$ME"
    echo "  added $ME to dialout — a REBOOT (or full re-login) is required"
  fi
  echo
fi

echo "==> Verification"

for p in "${PKGS[@]}"; do
  if dpkg -s "$p" >/dev/null 2>&1; then ok "package $p"; else bad "package $p missing"; fi
done

if python3 -c 'import serial' 2>/dev/null; then
  ok "python3 can import serial (pyserial)"
else
  bad "python3 cannot import serial — solvend-serial.py will not start"
fi

# The clock check is the one that matters most. `timedatectl show` is parsed
# rather than the human-readable output, which changes between OS releases.
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
  ok "system clock synchronised"
else
  bad "clock NOT synchronised — every OTP expiry will be wrong. Wait for NTP, then re-check."
fi
printf '        timezone now: %s | time: %s\n' \
  "$(timedatectl show -p Timezone --value 2>/dev/null)" "$(date -Is)"

# Group membership has two states: granted, and granted-but-not-yet-active.
# Only the current process's groups tell you whether a reboot is still pending.
if id -nG "$ME" | grep -qw dialout; then
  ok "dialout active in the current session"
elif getent group dialout | grep -qw "$ME"; then
  bad "dialout granted but NOT active — reboot, then re-run with --check"
else
  bad "$ME is not in dialout — serial bridge will be denied /dev/ttyUSB0"
fi

# Informational: the ESP32 is usually not plugged in during Phase 4.
if ls /dev/serial/by-id/* >/dev/null 2>&1; then
  ok "serial device present: $(ls /dev/serial/by-id/* | head -1)"
else
  note "no /dev/serial/by-id/* yet — expected until the ESP32 is connected (Phase 7/8)"
fi

printf '\n%d ok, %d failed, %d warnings\n' "$pass" "$fail" "$warn"
if [ "$fail" -gt 0 ]; then
  echo "Phase 4 is NOT complete. Fix the FAIL lines above before Phase 5."
  exit 1
fi
echo "Phase 4 base is ready. Next: Phase 5 (ZeroClaw install)."
