#!/usr/bin/env bash
# lib/60-verify-local.sh — verify the instance is actually up, from this
# machine's own localhost (this proves the server, not the access path — see
# recovery/RECOVERY.md for the cross-network check that proves the access
# path).
#
# Checks:
#   1. systemctl is-active atlas-mcp
#   2. systemctl is-active atlas-session-retention.timer
#   3. curl a GET to the MCP endpoint on localhost. A bare GET to a
#      streamable-http MCP endpoint is expected to fail with HTTP 406 and a
#      JSON-RPC body naming "text/event-stream" — that is what a healthy,
#      OAuth-enabled atlas-mcp actually returns for this call (127.0.0.1
#      bypasses auth, but the route only accepts a proper MCP session, not a
#      bare GET). Anything else (connection refused, 401, 404, empty body)
#      means the server did not come up correctly.
#   4. .venv/bin/python tools/validate.py exits 0.
#
# Usage: ./lib/60-verify-local.sh [--port 8105]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

PORT=8105
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    *) die "unrecognized argument: $1" ;;
  esac
done

banner "Verifying atlas-mcp is healthy (local checks only)"

fail=0

check_unit() {
  local unit="$1"
  if systemctl is-active --quiet "$unit"; then
    echo "ACTIVE   $unit"
  else
    echo "NOT ACTIVE   $unit ($(systemctl is-active "$unit" || true))"
    fail=1
  fi
}
check_unit atlas-mcp
check_unit atlas-session-retention.timer

mcp_body_file="$(mktemp)"
code="$(curl -s -o "$mcp_body_file" -w '%{http_code}' "http://127.0.0.1:${PORT}/mcp")"
payload="$(cat "$mcp_body_file")"
rm -f "$mcp_body_file"
if [ "$code" = "406" ] && printf '%s' "$payload" | grep -q "text/event-stream"; then
  echo "MCP_ENDPOINT_OK   GET /mcp -> 406 (text/event-stream required, as expected for a live streamable-http server)"
else
  echo "MCP_ENDPOINT_FAIL   GET /mcp -> $code, body: $payload"
  fail=1
fi

cd "$REPO_ROOT"
validate_out="$(mktemp)"
if .venv/bin/python tools/validate.py >"$validate_out" 2>&1; then
  echo "VALIDATE_OK"
else
  echo "VALIDATE_FAILED:"
  cat "$validate_out"
  fail=1
fi
rm -f "$validate_out"

[ "$fail" -eq 0 ] || die "one or more local health checks failed — see the report above"
log "all local health checks passed"
