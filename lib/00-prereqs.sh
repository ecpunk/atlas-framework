#!/usr/bin/env bash
# lib/00-prereqs.sh — verify this machine has what the rest of the phases
# need. Fails with a plain-language message naming exactly what is missing
# and the apt package that provides it.
#
# Usage: ./lib/00-prereqs.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

banner "Checking prerequisites on $(hostname -s)"

fail=0

check_bin() {
  local name="$1" pkg="$2"
  if command -v "$name" >/dev/null 2>&1; then
    printf 'FOUND %-8s %s\n' "$name" "$("$name" --version 2>&1 | head -n1)"
  else
    printf 'MISSING %-8s (install with: sudo apt-get install -y %s)\n' "$name" "$pkg"
    fail=1
  fi
}

check_bin git git
check_bin curl curl
if command -v openssl >/dev/null 2>&1; then
  printf 'FOUND %-8s %s\n' openssl "$(openssl version 2>&1 | head -n1)"
else
  printf 'MISSING %-8s (install with: sudo apt-get install -y openssl)\n' openssl
  fail=1
fi

if command -v python3 >/dev/null 2>&1; then
  pyver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  major="$(python3 -c 'import sys; print(sys.version_info[0])')"
  minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
  if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
    printf 'FOUND    python3  %s (>= 3.10, OK)\n' "$pyver"
  else
    printf 'TOO OLD  python3  %s (need >= 3.10)\n' "$pyver"
    fail=1
  fi
  if ! python3 -m venv --help >/dev/null 2>&1; then
    printf 'MISSING  python3-venv module (install with: sudo apt-get install -y python3-venv)\n'
    fail=1
  fi
else
  printf 'MISSING  python3  (install with: sudo apt-get install -y python3 python3-venv)\n'
  fail=1
fi

if command -v sudo >/dev/null 2>&1; then
  printf 'FOUND    sudo     (phase 50 installs systemd units with it — expect a password prompt)\n'
else
  printf 'MISSING  sudo     (phase 50 needs it to install systemd units)\n'
  fail=1
fi

[ "$fail" -eq 0 ] || die "one or more prerequisites are missing — see the report above for install commands"
log "all prerequisites present"
