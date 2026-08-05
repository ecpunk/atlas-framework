#!/usr/bin/env bash
# lib/20-venv-deps.sh — create the instance's own .venv and install
# requirements.txt into it. Idempotent: venv creation and pip install are
# both safe to re-run.
#
# Usage: ./lib/20-venv-deps.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

banner "Creating venv + installing dependencies in $REPO_ROOT"

cd "$REPO_ROOT"
[ -f requirements.txt ] || die "no requirements.txt at $(pwd) — is this the engine repo root?"

if [ -x .venv/bin/python3 ]; then
  log ".venv already exists — reusing it"
else
  python3 -m venv .venv
  log "created .venv"
fi

.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
log "dependencies installed:"
.venv/bin/pip freeze | grep -iE '^(pydantic|pyyaml|mcp)=' | sed 's/^/  /'
