#!/usr/bin/env bash
# lib/50-systemd-units.sh — render systemd/*.tmpl for THIS machine, install
# them with sudo, daemon-reload, and enable --now atlas-mcp + the
# session-retention timer. Idempotent: re-rendering and re-enabling
# already-enabled units is a no-op from systemd's point of view.
#
# Usage:
#   ./lib/50-systemd-units.sh --principal P --issuer URL \
#       [--resource URL] [--port 8105] [--instance-name NAME] [--group G]
#
# --resource defaults to --issuer (issuer == resource is the common case for
# a self-hosted single-operator OAuth setup).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

PORT=8105
PRINCIPAL=""
ISSUER=""
RESOURCE=""
GROUP=""
INSTANCE_NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --principal) PRINCIPAL="$2"; shift 2 ;;
    --issuer) ISSUER="$2"; shift 2 ;;
    --resource) RESOURCE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
    *) die "unrecognized argument: $1" ;;
  esac
done

[ -n "$PRINCIPAL" ] || die "missing required --principal"
[ -n "$ISSUER" ] || die "missing required --issuer"
[ -n "$RESOURCE" ] || RESOURCE="$ISSUER"
[ -n "$GROUP" ] || GROUP="$(id -gn)"
[ -n "$INSTANCE_NAME" ] || INSTANCE_NAME="$(hostname -s)"

RUN_USER="$(id -un)"
RUN_HOME="$HOME"

banner "Installing systemd units on $(hostname -s) (user=$RUN_USER, port=$PORT)"

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

render() {
  local src="$1" dst="$2"
  sed \
    -e "s#__TARGET_USER__#${RUN_USER}#g" \
    -e "s#__TARGET_GROUP__#${GROUP}#g" \
    -e "s#__TARGET_HOME__#${RUN_HOME}#g" \
    -e "s#__ATLAS_REPO_ROOT__#${REPO_ROOT}#g" \
    -e "s#__ATLAS_PORT__#${PORT}#g" \
    -e "s#__ATLAS_OAUTH_ISSUER__#${ISSUER}#g" \
    -e "s#__ATLAS_OAUTH_RESOURCE__#${RESOURCE}#g" \
    -e "s#__ATLAS_OAUTH_PRINCIPAL__#${PRINCIPAL}#g" \
    -e "s#__ATLAS_INSTANCE_NAME__#${INSTANCE_NAME}#g" \
    "$src" > "$dst"
}

render "$REPO_ROOT/systemd/atlas-mcp.service.tmpl" "$RENDER_DIR/atlas-mcp.service"
render "$REPO_ROOT/systemd/atlas-session-retention.service.tmpl" "$RENDER_DIR/atlas-session-retention.service"
cp "$REPO_ROOT/systemd/atlas-session-retention.timer" "$RENDER_DIR/atlas-session-retention.timer"

log "rendered units; installing to /etc/systemd/system (sudo may prompt for your password)"
sudo install -m 0644 "$RENDER_DIR/atlas-mcp.service" /etc/systemd/system/atlas-mcp.service
sudo install -m 0644 "$RENDER_DIR/atlas-session-retention.service" /etc/systemd/system/atlas-session-retention.service
sudo install -m 0644 "$RENDER_DIR/atlas-session-retention.timer" /etc/systemd/system/atlas-session-retention.timer
sudo systemctl daemon-reload
sudo systemctl enable --now atlas-mcp
sudo systemctl enable --now atlas-session-retention.timer

log "systemd units installed and enabled"
