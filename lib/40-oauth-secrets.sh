#!/usr/bin/env bash
# lib/40-oauth-secrets.sh — create .secrets/ (0700) and generate
# oauth_login_secret.txt (0600) if it does not already exist. Never
# overwrites an existing secret silently. Prints the secret exactly once,
# with a "store this" warning, and never logs it anywhere else.
#
# Usage: ./lib/40-oauth-secrets.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

banner "Provisioning OAuth secrets in $REPO_ROOT"

cd "$REPO_ROOT"
mkdir -p .secrets
chmod 700 .secrets

SECRET_FILE=".secrets/oauth_login_secret.txt"
if [ -f "$SECRET_FILE" ]; then
  log "SECRET_ALREADY_EXISTS: $SECRET_FILE — leaving it untouched."
  log "(Run recovery/reset-login-secret.sh if it needs to be rotated.)"
else
  secret="$(openssl rand -base64 24)"
  umask 077
  printf '%s\n' "$secret" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  echo "SECRET_GENERATED"
  echo "=============================================================="
  echo "  Atlas OAuth login secret (STORE THIS — it is shown only once):"
  echo
  echo "  $secret"
  echo
  echo "  File: $REPO_ROOT/$SECRET_FILE (mode 600)"
  echo "=============================================================="
fi
