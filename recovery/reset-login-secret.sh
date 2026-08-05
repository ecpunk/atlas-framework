#!/usr/bin/env bash
# reset-login-secret.sh — regenerate the Atlas login code and restart the
# server. Lives at <atlas-store>/recovery/reset-login-secret.sh on the
# target box. Run it there directly:
#
#   ./recovery/reset-login-secret.sh
#
# See ../RECOVERY.md ("if Claude stops connecting") for when to use this.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECRET_FILE="$REPO_ROOT/.secrets/oauth_login_secret.txt"

mkdir -p "$REPO_ROOT/.secrets"
chmod 700 "$REPO_ROOT/.secrets"

echo "Generating a new connection code..."
new_secret="$(openssl rand -base64 24)"
umask 077
printf '%s\n' "$new_secret" > "$SECRET_FILE"
chmod 600 "$SECRET_FILE"

echo "Restarting the Atlas server so it picks up..."
sudo systemctl restart atlas-mcp
sleep 2
if systemctl is-active --quiet atlas-mcp; then
  echo "The server is back up."
else
  echo "The server did not come back up. Run: sudo systemctl status atlas-mcp"
fi

cat <<EOF

==============================================================
  Your new connection code (write this down / save it now —
  it will not be shown again):

  $new_secret

  You will need this the next time you (re)connect a device to
  Atlas — paste it in when it asks you to log in.
==============================================================
EOF
