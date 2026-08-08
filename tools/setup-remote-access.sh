#!/usr/bin/env bash
# tools/setup-remote-access.sh — make this Atlas instance reachable from
# your phone or any browser, using your own Cloudflare account.
#
# Everything here is driven by the Cloudflare API through one API token you
# create yourself — no `cloudflared tunnel login`, no browser step, no
# cert.pem, no local config.yml. The connector authenticates with a run
# token instead, and every setting (the tunnel, its ingress rule, the DNS
# record, the connector service) is created or updated through the API, so
# this is safe to re-run any time.
#
# Before running this, create ONE Cloudflare API token at
#   https://dash.cloudflare.com/profile/api-tokens
# ("Create Token" -> "Create Custom Token") scoped to exactly:
#   - Account -> Cloudflare Tunnel: Edit
#   - Zone    -> DNS: Edit
#   - Zone    -> WAF (rate limiting): Edit   [optional — used for one
#     best-effort protection later; everything else works without it]
#
# The domain you want Atlas under must already be added to that Cloudflare
# account. This script never touches anyone else's infrastructure and has
# no estate-specific defaults baked in.
#
# Usage:
#   ./tools/setup-remote-access.sh [--hostname atlas.yourdomain.com] [--port PORT]
#
# --hostname   the public hostname you want Atlas reachable at. Must be a
#              subdomain of a domain already added to your Cloudflare
#              account. Optional — if you have exactly one domain on the
#              account, it defaults to atlas.<that domain>; with more than
#              one, the script asks you to pick.
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

command -v curl >/dev/null 2>&1 || die "curl is required and not on PATH"
command -v python3 >/dev/null 2>&1 || die "python3 is required and not on PATH"

TUNNEL_NAME="atlas"
ATLAS_UNIT="/etc/systemd/system/atlas-mcp.service"
CF_API="https://api.cloudflare.com/client/v4"
TOKEN_FILE="$REPO_ROOT/.secrets/cf_working_token.txt"
ENV_FILE="/etc/cloudflared/atlas-tunnel.env"
ATLAS_TUNNEL_UNIT="atlas-cloudflared.service"
ATLAS_TUNNEL_UNIT_PATH="/etc/systemd/system/${ATLAS_TUNNEL_UNIT}"

WORK_DIR="$(mktemp -d)"
chmod 700 "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT
CURL_HDR_FILE="$WORK_DIR/cf_headers.cfg"

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
    -h|--help) sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unrecognized argument: $1 (see --help)" ;;
  esac
done

[ -n "$PORT" ] || PORT="$(discover_port)"

# ---------------------------------------------------------------------------
# Cloudflare API helpers
# ---------------------------------------------------------------------------

# write_cf_headers TOKEN — (re)writes the curl config file that carries the
# bearer token as a header. Passed to curl via -K so the token never appears
# as a command-line argument (and so never shows up in `ps`).
write_cf_headers() {
  : > "$CURL_HDR_FILE"
  chmod 600 "$CURL_HDR_FILE"
  printf 'header = "Authorization: Bearer %s"\n' "$1" >> "$CURL_HDR_FILE"
  printf 'header = "Content-Type: application/json"\n' >> "$CURL_HDR_FILE"
}

# cf_api METHOD PATH [DATA_FILE] — calls the Cloudflare API. Sets CF_STATUS
# (HTTP status code, or "000" on a network-level failure) and CF_BODY (the
# response body). Never dies on its own — callers decide via cf_ok/CF_STATUS,
# so best-effort steps can fail without aborting the script.
cf_api() {
  local method="$1" path="$2" data_file="${3:-}" raw
  if [ -n "$data_file" ]; then
    raw="$(curl -sS -K "$CURL_HDR_FILE" -X "$method" --data-binary "@${data_file}" -w $'\n%{http_code}' "${CF_API}${path}" || printf '\n000')"
  else
    raw="$(curl -sS -K "$CURL_HDR_FILE" -X "$method" -w $'\n%{http_code}' "${CF_API}${path}" || printf '\n000')"
  fi
  CF_STATUS="$(printf '%s\n' "$raw" | tail -n1)"
  CF_BODY="$(printf '%s\n' "$raw" | head -n -1)"
}

cf_ok() { case "$CF_STATUS" in 2??) return 0 ;; *) return 1 ;; esac; }

# list_id_name JSON_BODY — prints "id|name" one per line from a {"result":[...]}
# list response. Silent (no output) on any parse error.
list_id_name() {
  printf '%s' "$1" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in (data.get("result") or []):
    i, n = item.get("id"), item.get("name")
    if i and n:
        print(f"{i}|{n}")
'
}

# find_zone_match JSON_BODY HOSTNAME — prints "id|name" of the zone whose
# name is the longest suffix match of HOSTNAME, or nothing if none match.
find_zone_match() {
  printf '%s' "$1" | python3 -c '
import json, sys
hostname = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
zones = data.get("result") or []
candidates = [z for z in zones if z.get("name") and (hostname == z["name"] or hostname.endswith("." + z["name"]))]
if not candidates:
    sys.exit(0)
candidates.sort(key=lambda z: len(z["name"]), reverse=True)
best = candidates[0]
best_id, best_name = best["id"], best["name"]
print(f"{best_id}|{best_name}")
' "$2"
}

verify_token() {
  cf_api GET "/user/tokens/verify"
  [ "$CF_STATUS" = "200" ] || return 1
  local ok
  ok="$(printf '%s' "$CF_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("no"); sys.exit()
result = d.get("result") or {}
print("yes" if d.get("success") and result.get("status") == "active" else "no")
')"
  [ "$ok" = "yes" ]
}

get_api_token() {
  mkdir -p "$REPO_ROOT/.secrets"
  chmod 700 "$REPO_ROOT/.secrets"

  local token=""
  if [ -f "$TOKEN_FILE" ]; then
    log "found a previously saved Cloudflare API token — re-verifying it"
    token="$(cat "$TOKEN_FILE")"
    write_cf_headers "$token"
    if verify_token; then
      log "saved token is still valid, reusing it"
    else
      log "saved token no longer verifies — you'll need to paste a new one"
      token=""
    fi
  fi

  if [ -z "$token" ]; then
    cat <<'EOF'

You need a Cloudflare API token scoped to exactly:
  - Account -> Cloudflare Tunnel: Edit
  - Zone    -> DNS: Edit
  - Zone    -> WAF (rate limiting): Edit   [optional]

Create one at https://dash.cloudflare.com/profile/api-tokens ("Create
Token" -> "Create Custom Token"), then paste it below. Typing is hidden —
nothing will appear on screen as you paste or type it.
EOF
    read -r -s -p "Cloudflare API token: " token
    echo
    [ -n "$token" ] || die "no token entered"
    write_cf_headers "$token"
    verify_token || die "that token did not verify against the Cloudflare API — check it was copied in full and re-run this script"
    umask 077
    printf '%s\n' "$token" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    log "token verified — saved to $TOKEN_FILE (0600) so you won't need to paste it again"
  fi
}

discover_account() {
  log "looking up your Cloudflare account"
  cf_api GET "/accounts"
  cf_ok || die "could not list Cloudflare accounts (HTTP $CF_STATUS) — check the token is valid and has account-level access"
  mapfile -t _accounts < <(list_id_name "$CF_BODY")
  case "${#_accounts[@]}" in
    0) die "this token can't see any Cloudflare account — check the scopes when you created it" ;;
    1)
      IFS='|' read -r ACCOUNT_ID ACCOUNT_NAME <<<"${_accounts[0]}"
      ;;
    *)
      log "this token has access to more than one Cloudflare account:"
      local a n
      for a in "${_accounts[@]}"; do
        IFS='|' read -r _ n <<<"$a"
        log "  - $n"
      done
      die "this script only supports a token scoped to exactly one account — recreate the token under a single account and re-run"
      ;;
  esac
  log "using Cloudflare account: $ACCOUNT_NAME"
}

discover_zone_and_hostname() {
  log "looking up active domains on this Cloudflare account"
  cf_api GET "/zones?status=active"
  cf_ok || die "could not list Cloudflare zones (HTTP $CF_STATUS) — check the token's Zone permissions"
  mapfile -t _zones < <(list_id_name "$CF_BODY")
  [ "${#_zones[@]}" -gt 0 ] || die "no active domains found on this Cloudflare account — add your domain in the Cloudflare dashboard first, then re-run this script"

  if [ -n "$HOSTNAME_ARG" ]; then
    local match
    match="$(find_zone_match "$CF_BODY" "$HOSTNAME_ARG")"
    [ -n "$match" ] || die "no domain on this Cloudflare account covers ${HOSTNAME_ARG} — add that domain to Cloudflare first, or drop --hostname to pick from what's available"
    IFS='|' read -r ZONE_ID ZONE_NAME <<<"$match"
  elif [ "${#_zones[@]}" -eq 1 ]; then
    IFS='|' read -r ZONE_ID ZONE_NAME <<<"${_zones[0]}"
    HOSTNAME_ARG="atlas.${ZONE_NAME}"
  else
    log "this Cloudflare account has more than one domain:"
    local i=1 z n
    for z in "${_zones[@]}"; do
      IFS='|' read -r _ n <<<"$z"
      log "  $i) $n"
      i=$((i + 1))
    done
    local choice
    read -r -p "Which domain should Atlas live under? [1-${#_zones[@]}]: " choice
    case "$choice" in
      ''|*[!0-9]*) die "invalid choice — re-run this script and enter a number from the list" ;;
    esac
    { [ "$choice" -ge 1 ] && [ "$choice" -le "${#_zones[@]}" ]; } || die "invalid choice — re-run this script and enter a number from the list"
    IFS='|' read -r ZONE_ID ZONE_NAME <<<"${_zones[$((choice - 1))]}"
    HOSTNAME_ARG="atlas.${ZONE_NAME}"
  fi
  log "using hostname: ${HOSTNAME_ARG} (zone: ${ZONE_NAME})"
}

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

ensure_tunnel() {
  log "looking for an existing tunnel named '$TUNNEL_NAME'"
  cf_api GET "/accounts/${ACCOUNT_ID}/cfd_tunnel?name=${TUNNEL_NAME}&is_deleted=false"
  cf_ok || die "could not list tunnels (HTTP $CF_STATUS): $CF_BODY"
  mapfile -t _tunnels < <(list_id_name "$CF_BODY")
  if [ "${#_tunnels[@]}" -gt 0 ]; then
    IFS='|' read -r TUNNEL_ID _ <<<"${_tunnels[0]}"
    log "found existing tunnel (id $TUNNEL_ID), reusing it"
    return
  fi
  log "creating tunnel '$TUNNEL_NAME'"
  printf '%s' '{"name":"atlas","config_src":"cloudflare"}' > "$WORK_DIR/create_tunnel.json"
  cf_api POST "/accounts/${ACCOUNT_ID}/cfd_tunnel" "$WORK_DIR/create_tunnel.json"
  cf_ok || die "tunnel create failed (HTTP $CF_STATUS): $CF_BODY"
  TUNNEL_ID="$(printf '%s' "$CF_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
i = (d.get("result") or {}).get("id")
if i:
    print(i)
')"
  [ -n "$TUNNEL_ID" ] || die "tunnel create appeared to succeed but no tunnel id was returned"
  log "tunnel id: $TUNNEL_ID"
}

push_ingress() {
  log "setting the tunnel's ingress rule (${HOSTNAME_ARG} -> 127.0.0.1:${PORT})"
  python3 -c '
import json, sys
hostname, port = sys.argv[1], sys.argv[2]
print(json.dumps({"config": {"ingress": [
    {"hostname": hostname, "service": f"http://127.0.0.1:{port}"},
    {"service": "http_status:404"},
]}}))
' "$HOSTNAME_ARG" "$PORT" > "$WORK_DIR/ingress.json"
  cf_api PUT "/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/configurations" "$WORK_DIR/ingress.json"
  cf_ok || die "failed to set the tunnel's ingress config (HTTP $CF_STATUS): $CF_BODY"
}

fetch_run_token() {
  log "fetching the tunnel's connector run token"
  cf_api GET "/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/token"
  cf_ok || die "failed to fetch the tunnel run token (HTTP $CF_STATUS)"
  RUN_TOKEN="$(printf '%s' "$CF_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
r = d.get("result")
if isinstance(r, str) and r:
    print(r)
')"
  [ -n "$RUN_TOKEN" ] || die "could not read the tunnel run token from the Cloudflare API response"
}

route_dns() {
  local desired_content="${TUNNEL_ID}.cfargotunnel.com"
  log "routing ${HOSTNAME_ARG} -> ${desired_content}"
  cf_api GET "/zones/${ZONE_ID}/dns_records?type=CNAME&name%5Bexact%5D=${HOSTNAME_ARG}"
  cf_ok || die "could not look up existing DNS records (HTTP $CF_STATUS): $CF_BODY"
  local existing
  existing="$(printf '%s' "$CF_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
r = d.get("result") or []
if r:
    rec = r[0]
    rec_id = rec.get("id", "")
    rec_content = rec.get("content", "")
    rec_proxied = str(bool(rec.get("proxied")))
    print(f"{rec_id}|{rec_content}|{rec_proxied}")
')"
  python3 -c '
import json, sys
hostname, content = sys.argv[1], sys.argv[2]
print(json.dumps({"type": "CNAME", "name": hostname, "content": content, "proxied": True}))
' "$HOSTNAME_ARG" "$desired_content" > "$WORK_DIR/dns.json"

  if [ -n "$existing" ]; then
    local rec_id rec_content rec_proxied
    IFS='|' read -r rec_id rec_content rec_proxied <<<"$existing"
    if [ "$rec_content" = "$desired_content" ] && [ "$rec_proxied" = "True" ]; then
      log "DNS record already correct, skipping"
      return
    fi
    log "updating existing DNS record to point at this tunnel"
    cf_api PUT "/zones/${ZONE_ID}/dns_records/${rec_id}" "$WORK_DIR/dns.json"
    cf_ok || die "failed to update the DNS record (HTTP $CF_STATUS): $CF_BODY"
  else
    log "creating DNS record"
    cf_api POST "/zones/${ZONE_ID}/dns_records" "$WORK_DIR/dns.json"
    cf_ok || die "failed to create the DNS record (HTTP $CF_STATUS): $CF_BODY"
  fi
}

install_systemd_unit() {
  local cloudflared_bin
  cloudflared_bin="$(command -v cloudflared)" || die "cloudflared is not on PATH"

  log "installing the cloudflared connector service (sudo may prompt for your password)"
  sudo install -d -m 755 /etc/cloudflared
  # Create 0600 BEFORE writing so the token is never world-readable, even
  # for an instant.
  sudo install -m 600 -o root -g root /dev/null "$ENV_FILE"
  sudo tee "$ENV_FILE" >/dev/null <<EOF
TUNNEL_TOKEN=${RUN_TOKEN}
EOF

  sudo tee "$ATLAS_TUNNEL_UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Cloudflare Tunnel for Atlas (token-managed; no cert.pem or config.yml)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
# cloudflared reads TUNNEL_TOKEN from the environment natively — passing it
# as --token would put the credential in ps-visible argv.
EnvironmentFile=${ENV_FILE}
ExecStart=${cloudflared_bin} tunnel run
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "$ATLAS_TUNNEL_UNIT" >/dev/null 2>&1 || true
  log "restarting the connector service to pick up the current config (sudo may prompt for your password)"
  sudo systemctl restart "$ATLAS_TUNNEL_UNIT"
  sleep 2
  systemctl is-active --quiet "$ATLAS_TUNNEL_UNIT" || die "the cloudflared connector service did not start — check: sudo systemctl status $ATLAS_TUNNEL_UNIT"
  log "connector service is active"
}

attempt_ratelimit() {
  local rule_desc="atlas-oauth-login-ratelimit"
  log "attempting an optional rate-limit rule on ${HOSTNAME_ARG}/oauth/login (skipped automatically if the token wasn't given WAF rate-limiting scope)"
  cf_api GET "/zones/${ZONE_ID}/rulesets/phases/http_ratelimit/entrypoint"

  local body=""
  if [ "$CF_STATUS" = "200" ]; then
    body="$(printf '%s' "$CF_BODY" | python3 -c '
import json, sys
hostname, desc = sys.argv[1], sys.argv[2]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
rules = (d.get("result") or {}).get("rules") or []
if any(r.get("description") == desc for r in rules):
    print("SKIP")
else:
    rules.append({
        "description": desc,
        "expression": f"(http.request.uri.path eq \"/oauth/login\") and (http.host eq \"{hostname}\")",
        "action": "block",
        "ratelimit": {
            "characteristics": ["cf.colo.id", "ip.src"],
            "period": 10,
            "requests_per_period": 10,
            "mitigation_timeout": 600,
        },
    })
    print(json.dumps({"rules": rules}))
' "$HOSTNAME_ARG" "$rule_desc")" || body=""
    if [ "$body" = "SKIP" ]; then
      log "rate-limit rule already present, skipping"
      return
    fi
  elif [ "$CF_STATUS" = "404" ]; then
    body="$(python3 -c '
import json, sys
hostname, desc = sys.argv[1], sys.argv[2]
rule = {
    "description": desc,
    "expression": f"(http.request.uri.path eq \"/oauth/login\") and (http.host eq \"{hostname}\")",
    "action": "block",
    "ratelimit": {
        "characteristics": ["cf.colo.id", "ip.src"],
        "period": 10,
        "requests_per_period": 10,
        "mitigation_timeout": 600,
    },
}
print(json.dumps({"rules": [rule]}))
' "$HOSTNAME_ARG" "$rule_desc")"
  else
    log "rate limiting on /oauth/login was skipped (HTTP $CF_STATUS) — this protection is optional, everything else is set up. Grant the token WAF rate-limiting edit and re-run this script if you want it."
    return
  fi

  if [ -z "$body" ]; then
    log "rate limiting on /oauth/login was skipped (could not read the current rate-limit rules) — this protection is optional, everything else is set up"
    return
  fi

  printf '%s' "$body" > "$WORK_DIR/ratelimit.json"
  cf_api PUT "/zones/${ZONE_ID}/rulesets/phases/http_ratelimit/entrypoint" "$WORK_DIR/ratelimit.json"
  if cf_ok; then
    log "rate-limit rule on /oauth/login is in place"
  else
    log "rate limiting on /oauth/login was skipped (HTTP $CF_STATUS) — this protection is optional, everything else is set up"
  fi
}

rerun_phase50() {
  local principal
  principal="$(discover_principal)"
  log "re-running bootstrap.sh --phase 50 --issuer https://${HOSTNAME_ARG} --principal ${principal}"
  "$REPO_ROOT/bootstrap.sh" --phase 50 --issuer "https://${HOSTNAME_ARG}" \
      --principal "$principal" --port "$PORT" \
    || die "bootstrap.sh --phase 50 failed — fix the issue above, then re-run this script"
}

banner "Setting up remote access for Atlas (local port ${PORT})"

cat <<EOF
This makes Atlas reachable from your phone or any browser, through your
own Cloudflare account — entirely through the Cloudflare API, using one
API token you create. No browser login, no cert.pem, no local config.yml.

Everything here is scripted and safe to re-run.
EOF

get_api_token
discover_account
discover_zone_and_hostname
install_cloudflared
ensure_tunnel
push_ingress
fetch_run_token
route_dns
install_systemd_unit
attempt_ratelimit
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

 Your Cloudflare API token is stored at $TOKEN_FILE (0600) — it's only
 used locally, to make Cloudflare API calls when you re-run this script.
 To revoke or rotate it, delete or edit it in the Cloudflare dashboard
 (https://dash.cloudflare.com/profile/api-tokens), or with your master
 token, then re-run this script and paste in the replacement.
==========================================================================
EOF
