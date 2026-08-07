#!/usr/bin/env bash
# tools/setup-remote-access.sh — make this Atlas instance reachable from
# your phone or any browser, using your own Cloudflare account.
#
# This scripts the whole ceremony except two genuinely human moments:
#   1. Having a free Cloudflare account with your domain already added to it.
#   2. One browser login when this script runs `cloudflared tunnel login`.
#
# Everything else is scripted and safe to re-run: installing cloudflared,
# creating a tunnel named "atlas", routing DNS for your chosen hostname,
# writing the tunnel config, installing the cloudflared system service, and
# finally re-running ./bootstrap.sh --phase 50 so the OAuth issuer picks up
# your new public URL.
#
# This runs on YOUR box against YOUR Cloudflare account. It never touches
# anyone else's infrastructure and has no estate-specific defaults baked in.
#
# Usage:
#   ./tools/setup-remote-access.sh --hostname atlas.yourdomain.com [--port PORT]
#
# --hostname   the public hostname you want Atlas reachable at. Must be a
#              subdomain of a domain already added to your Cloudflare
#              account (e.g. atlas.yourdomain.com).
# --port       local port Atlas is listening on. Default: discovered from
#              the installed atlas-mcp systemd unit if present, else 8105
#              (bootstrap.sh's own default).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ ! -f "$REPO_ROOT/lib/_common.sh" ]; then
  echo "[setup-remote-access] ERROR: lib/_common.sh not found — run this from inside the Atlas repo (tools/setup-remote-access.sh)" >&2
  exit 1
fi
# shellcheck source=../lib/_common.sh
source "$REPO_ROOT/lib/_common.sh"

[ -x "$REPO_ROOT/bootstrap.sh" ] || die "bootstrap.sh not found at $REPO_ROOT — run this from inside the Atlas repo"

TUNNEL_NAME="atlas"
ATLAS_UNIT="/etc/systemd/system/atlas-mcp.service"

discover_port() {
  if [ -f "$ATLAS_UNIT" ]; then
    local p
    p="$(sed -n 's/^Environment=ATLAS_MCP_PORT=//p' "$ATLAS_UNIT" | head -n1)"
    [ -n "$p" ] && { printf '%s' "$p"; return; }
  fi
  printf '8105'
}

discover_principal() {
  if [ -f "$ATLAS_UNIT" ]; then
    local p
    p="$(sed -n 's/^Environment=ATLAS_OAUTH_PRINCIPAL=//p' "$ATLAS_UNIT" | head -n1)"
    [ -n "$p" ] && { printf '%s' "$p"; return; }
  fi
  printf 'operator'
}

HOSTNAME_ARG=""
PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) HOSTNAME_ARG="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unrecognized argument: $1 (see --help)" ;;
  esac
done

[ -n "$HOSTNAME_ARG" ] || die "missing required --hostname (e.g. --hostname atlas.yourdomain.com)"
[ -n "$PORT" ] || PORT="$(discover_port)"

banner "Setting up remote access for ${HOSTNAME_ARG} (local port ${PORT})"

cat <<EOF
This makes Atlas reachable from your phone or any browser, through your
own Cloudflare account. You need:

  - A free Cloudflare account with your domain already added to it.
  - One browser login below (cloudflared tunnel login) — a page will open;
    log in and approve the domain you want to use.

Everything else here is scripted and safe to re-run.
EOF

install_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    log "cloudflared already installed, skipping"
    return 0
  fi
  local arch deb_url tmp_deb
  arch="$(dpkg --print-architecture)"
  deb_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
  tmp_deb="$(mktemp --suffix=.deb)"
  log "downloading cloudflared (${arch}) from ${deb_url}"
  curl -fsSL "$deb_url" -o "$tmp_deb" || die "failed to download cloudflared from $deb_url"
  log "installing cloudflared (sudo may prompt for your password)"
  sudo dpkg -i "$tmp_deb" || die "cloudflared package install failed"
  rm -f "$tmp_deb"
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared install completed but the binary is not on PATH"
  log "cloudflared installed"
}

cloudflared_login() {
  local cert="$HOME/.cloudflared/cert.pem"
  if [ -f "$cert" ]; then
    log "already logged in to Cloudflare (found $cert), skipping"
    return 0
  fi
  log "opening a browser to log in to your Cloudflare account — approve the domain you want to use"
  cloudflared tunnel login || die "cloudflared tunnel login did not complete — re-run this script after logging in"
  [ -f "$cert" ] || die "login did not produce $cert — re-run this script"
}

get_tunnel_id() {
  cloudflared tunnel list --name "$TUNNEL_NAME" -o json 2>/dev/null \
    | python3 -c 'import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = None
print(data[0]["id"] if data else "")' 2>/dev/null || true
}

ensure_tunnel() {
  TUNNEL_ID="$(get_tunnel_id)"
  if [ -n "$TUNNEL_ID" ]; then
    log "tunnel '$TUNNEL_NAME' already exists (id $TUNNEL_ID), skipping create"
    return 0
  fi
  log "creating tunnel '$TUNNEL_NAME'"
  cloudflared tunnel create "$TUNNEL_NAME" || die "tunnel create failed"
  TUNNEL_ID="$(get_tunnel_id)"
  [ -n "$TUNNEL_ID" ] || die "tunnel create appeared to succeed but no tunnel id was found afterwards"
  log "tunnel id: $TUNNEL_ID"
}

route_dns() {
  log "routing ${HOSTNAME_ARG} to tunnel '$TUNNEL_NAME'"
  local out
  if out="$(cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME_ARG" 2>&1)"; then
    log "route created"
  elif printf '%s' "$out" | grep -qi 'already exists\|already configured'; then
    log "route already exists for ${HOSTNAME_ARG}, skipping"
  else
    die "failed to create DNS route: $out"
  fi
}

write_config() {
  local cred_file="$HOME/.cloudflared/${TUNNEL_ID}.json"
  [ -f "$cred_file" ] || die "expected tunnel credentials file not found: $cred_file"
  CLOUDFLARED_CONFIG="$HOME/.cloudflared/config.yml"
  cat > "$CLOUDFLARED_CONFIG" <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${cred_file}

ingress:
  - hostname: ${HOSTNAME_ARG}
    service: http://127.0.0.1:${PORT}
  - service: http_status:404
EOF
  log "wrote $CLOUDFLARED_CONFIG"
}

install_service() {
  if systemctl list-unit-files 2>/dev/null | grep -q '^cloudflared\.service'; then
    # `service install` copied the config to /etc/cloudflared/ at install
    # time; the running service reads THAT copy, so refresh it before the
    # restart or hostname/port changes never take effect.
    if [ -f /etc/cloudflared/config.yml ]; then
      sudo cp "$CLOUDFLARED_CONFIG" /etc/cloudflared/config.yml
    fi
    log "cloudflared service already installed — restarting to pick up any config changes (sudo may prompt for your password)"
    sudo systemctl restart cloudflared
    sudo systemctl enable cloudflared >/dev/null 2>&1 || true
  else
    log "installing the cloudflared system service (sudo may prompt for your password)"
    sudo cloudflared --config "$CLOUDFLARED_CONFIG" service install \
      || die "cloudflared service install failed"
  fi
  sudo systemctl enable --now cloudflared
  log "cloudflared service installed and running"
}

rerun_phase50() {
  local principal
  principal="$(discover_principal)"
  log "re-running bootstrap.sh --phase 50 --issuer https://${HOSTNAME_ARG} --principal ${principal}"
  "$REPO_ROOT/bootstrap.sh" --phase 50 --issuer "https://${HOSTNAME_ARG}" \
      --principal "$principal" --port "$PORT" \
    || die "bootstrap.sh --phase 50 failed — fix the issue above, then re-run this script"
}

install_cloudflared
cloudflared_login
ensure_tunnel
route_dns
write_config
install_service
rerun_phase50

cat <<EOF

==========================================================================
 Your Atlas is reachable at:

   https://${HOSTNAME_ARG}

 Connect your phone or browser's Claude connector to that URL. It will
 ask for a login code — that's the one printed once by phase 40 of
 bootstrap.sh. If you've lost it, rotate it yourself (don't rely on old
 terminal scrollback — it's only ever shown once):

   ./recovery/reset-login-secret.sh
==========================================================================
EOF
