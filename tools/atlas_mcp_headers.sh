#!/bin/sh
# atlas_mcp_headers.sh: MCP `headersHelper` for this host's atlas-mcp.
# Prints the Authorization header JSON from the 0600 local-agent token file
# atlas-mcp writes at startup, so no MCP config file carries the token.
here="$(cd "$(dirname "$0")/.." && pwd)"
f="${ATLAS_LOCAL_TOKEN_FILE:-$here/.secrets/local_agent_token.txt}"
tok="$(cat "$f" 2>/dev/null)" || tok=""
[ -n "$tok" ] || { echo '{}'; exit 0; }
printf '{"Authorization": "Bearer %s"}\n' "$tok"
