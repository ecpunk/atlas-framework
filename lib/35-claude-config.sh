#!/usr/bin/env bash
# lib/35-claude-config.sh — install fixtures/claude/CLAUDE.md to
# ~/.claude/CLAUDE.md.
#
# This is the ONE instruction-layer file that needs an install step: it lives
# outside the repo, so git cannot deliver or protect it. Everything else in
# the instruction layer (docs/kb/Start Here.md, docs/kb/How I Work.md,
# mcp-instructions.md) ships as ordinary tracked files already at their final
# paths — the clone delivers them, and `git pull` refuses to clobber local
# edits to them natively.
#
# Marker: .ai-bootstrap-claude-md-version (repo root, gitignored) records the
# sha256 we last shipped, so upgrades can tell "still ours" from "the owner
# customized it".
#
# Re-run semantics (already-done is a SKIP, not a refusal):
#   - destination matches what we last shipped (or is missing) -> install the
#     current fixture, update the marker.
#   - destination differs from what we last shipped (owner/agent edited it)
#     -> SKIP with a named warning; never auto-overwritten. --resync-claude-md
#     re-attempts after you restore or remove the file.
#
# Usage: ./lib/35-claude-config.sh [--resync-claude-md]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

RESYNC=0
PORT=8105
while [ $# -gt 0 ]; do
  case "$1" in
    --resync-claude-md) RESYNC=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) die "unrecognized argument: $1" ;;
  esac
done

banner "Installing Claude config (~/.claude/CLAUDE.md + local MCP wiring)"

cd "$REPO_ROOT"
FIXTURE="fixtures/claude/CLAUDE.md"
DEST="$HOME/.claude/CLAUDE.md"
MARKER=".ai-bootstrap-claude-md-version"

[ -f "$FIXTURE" ] || die "fixture missing from the clone: $FIXTURE"

sha_of() {
  if [ -f "$1" ]; then sha256sum "$1" | awk '{print $1}'; else printf ''; fi
}

fixture_hash="$(sha_of "$FIXTURE")"
current_hash="$(sha_of "$DEST")"
shipped_hash=""
[ -f "$MARKER" ] && shipped_hash="$(tail -n1 "$MARKER")"

install_it() {
  mkdir -p "$(dirname "$DEST")"
  cp "$FIXTURE" "$DEST"
  {
    if [ -f VERSION ]; then cat VERSION; else date -u +"shipped_at: %Y-%m-%dT%H:%M:%SZ"; fi
    printf '%s\n' "$fixture_hash"
  } > "$MARKER"
}

# ---- Local MCP wiring: sessions started in this folder get the instance's
# own Atlas automatically (project-scope .mcp.json; loopback needs no auth —
# the server injects a local token for 127.0.0.1 callers). Merge-not-clobber:
# other entries the owner/agent adds later are preserved.
PORT="$PORT" python3 - "$REPO_ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
port = os.environ["PORT"]

mcp_path = os.path.join(root, ".mcp.json")
cfg = {}
if os.path.exists(mcp_path):
    try:
        cfg = json.load(open(mcp_path))
    except Exception:
        cfg = {}
cfg.setdefault("mcpServers", {})["atlas"] = {
    "type": "http",
    "url": f"http://127.0.0.1:{port}/mcp",
}
json.dump(cfg, open(mcp_path, "w"), indent=2)

settings_dir = os.path.join(root, ".claude")
os.makedirs(settings_dir, exist_ok=True)
settings_path = os.path.join(settings_dir, "settings.json")
settings = {}
if os.path.exists(settings_path):
    try:
        settings = json.load(open(settings_path))
    except Exception:
        settings = {}
enabled = settings.setdefault("enabledMcpjsonServers", [])
if "atlas" not in enabled:
    enabled.append("atlas")
json.dump(settings, open(settings_path, "w"), indent=2)
PY
log "local Atlas MCP wired for sessions in $REPO_ROOT (.mcp.json -> 127.0.0.1:$PORT, pre-approved in .claude/settings.json)"

if [ -z "$current_hash" ]; then
  install_it
  log "installed: $DEST"
elif [ "$current_hash" = "$fixture_hash" ]; then
  install_it   # refresh the marker so future runs compare against this version
  log "already matches shipped content: $DEST"
elif [ -n "$shipped_hash" ] && [ "$current_hash" = "$shipped_hash" ]; then
  install_it
  log "updated: $DEST (was our previous version)"
else
  if [ "$RESYNC" = "1" ]; then
    log "WARNING: $DEST differs from anything we shipped (edited by owner/agent) — SKIPPED even under --resync-claude-md. Never auto-overwritten; remove it or restore the shipped version first."
  else
    log "WARNING: $DEST exists and differs from the shipped version — NOT overwritten. It may be a personal config; merge by hand or remove it and re-run."
  fi
fi
