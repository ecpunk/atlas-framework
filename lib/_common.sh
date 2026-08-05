#!/usr/bin/env bash
# lib/_common.sh — shared helpers for the local bootstrap phases.
# Sourced by every numbered phase script; never executed directly.
#
# Every phase runs ON this machine, from inside the cloned repo. Nothing here
# reaches over the network except package installs (pip) — provisioning is
# fully local by design: you clone the repo, you run bootstrap.sh, done.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/.." && pwd)"

PHASE_NAME="${PHASE_NAME:-$(basename "${BASH_SOURCE[1]:-phase}")}"

log()  { printf '[%s] %s\n' "$PHASE_NAME" "$1"; }
die()  { printf '[%s] ERROR: %s\n' "$PHASE_NAME" "$1" >&2; exit 1; }
banner() {
  printf '\n[%s] ==== %s ====\n' "$PHASE_NAME" "$1"
}
