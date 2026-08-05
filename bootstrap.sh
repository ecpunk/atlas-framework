#!/usr/bin/env bash
# bootstrap.sh — provision a complete, independent Atlas instance from this
# cloned repo, entirely on this machine.
#
#   git clone <repo-url> /opt/stack/atlas-store
#   cd /opt/stack/atlas-store
#   ./bootstrap.sh
#
# The clone you are standing in becomes the instance's live store repo: the
# engine (tools/, schemas/, vocabularies/) runs in place, entities/ fills
# with YOUR data (committed locally, never pushed anywhere), and a later
# `git pull && ./bootstrap.sh` is the upgrade path.
#
# Phases, in order (each is also runnable standalone from lib/):
#   00  prereqs        python3 >= 3.10 + venv, git, curl, openssl, sudo
#   10  verify-clone   sanity-check the checkout, set git identity + pull mode
#   20  venv-deps      .venv + requirements.txt
#   30  seed-store     seed/rules -> entities/rules (marker-guarded)
#   35  claude-config  fixtures/claude/CLAUDE.md -> ~/.claude/CLAUDE.md
#   40  oauth-secrets  generate the login secret (printed exactly once)
#   50  systemd-units  render + install + enable atlas-mcp and the retention
#                      timer (the one phase that needs sudo)
#   60  verify-local   health checks against localhost
#
# Everything is idempotent: re-running bootstrap.sh after a failure re-does
# the finished phases quickly (skips/no-ops) and picks up where it stopped.
#
# Usage:
#   ./bootstrap.sh [--instance-name NAME] [--principal P] [--port 8105]
#                  [--issuer URL] [--resource URL] [--phase NN]
#                  [--reseed] [--resync-claude-md] [--dry-run]
#
#   --instance-name  short name for this instance (default: this hostname).
#                    Becomes the local host id server entities reference.
#   --principal      the operator identity OAuth tokens are minted for
#                    (default: operator)
#   --issuer         the public HTTPS URL this instance will eventually be
#                    reached at. Fine to leave defaulted until you have a
#                    domain: it is just a string in token metadata, and
#                    re-running `./bootstrap.sh --phase 50 --issuer <real-url>
#                    --principal <p>` updates it later.
#   --phase NN       run a single phase instead of all of them
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$REPO_ROOT/lib"

log() { printf '[bootstrap] %s\n' "$1"; }
die() { printf '[bootstrap] ERROR: %s\n' "$1" >&2; exit 1; }

INSTANCE_NAME="$(hostname -s)"
PRINCIPAL="operator"
PORT=8105
ISSUER=""
RESOURCE=""
ONLY_PHASE=""
RESEED=0
RESYNC_CLAUDE_MD=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
    --principal) PRINCIPAL="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --issuer) ISSUER="$2"; shift 2 ;;
    --resource) RESOURCE="$2"; shift 2 ;;
    --phase) ONLY_PHASE="$2"; shift 2 ;;
    --reseed) RESEED=1; shift ;;
    --resync-claude-md) RESYNC_CLAUDE_MD=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unrecognized argument: $1 (see --help)" ;;
  esac
done

ISSUER_DEFAULTED=0
if [ -z "$ISSUER" ]; then
  ISSUER="https://atlas-mcp.${INSTANCE_NAME}.invalid"
  ISSUER_DEFAULTED=1
fi
[ -n "$RESOURCE" ] || RESOURCE="$ISSUER"

ALL_PHASES=(00 10 20 30 35 40 50 60)

phase_command() {
  local phase="$1"
  case "$phase" in
    00) CMD=("$LIB/00-prereqs.sh") ;;
    10) CMD=("$LIB/10-verify-clone.sh") ;;
    20) CMD=("$LIB/20-venv-deps.sh") ;;
    30) CMD=("$LIB/30-seed-store.sh")
        if [ "$RESEED" -eq 1 ]; then CMD+=(--reseed); fi ;;
    35) CMD=("$LIB/35-claude-config.sh")
        if [ "$RESYNC_CLAUDE_MD" -eq 1 ]; then CMD+=(--resync-claude-md); fi ;;
    40) CMD=("$LIB/40-oauth-secrets.sh") ;;
    50) CMD=("$LIB/50-systemd-units.sh" --principal "$PRINCIPAL" --issuer "$ISSUER" \
             --resource "$RESOURCE" --port "$PORT" --instance-name "$INSTANCE_NAME") ;;
    60) CMD=("$LIB/60-verify-local.sh" --port "$PORT") ;;
    *) die "unknown phase: $phase" ;;
  esac
}

PHASES=("${ALL_PHASES[@]}")
if [ -n "$ONLY_PHASE" ]; then
  PHASES=("$ONLY_PHASE")
fi

log "instance-name=$INSTANCE_NAME principal=$PRINCIPAL port=$PORT"
log "issuer=$ISSUER$([ "$ISSUER_DEFAULTED" -eq 1 ] && echo '  (placeholder — set the real one later via --phase 50)')"

for phase in "${PHASES[@]}"; do
  phase_command "$phase"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN would run: ${CMD[*]}"
    continue
  fi
  if ! "${CMD[@]}"; then
    die "phase $phase failed — fix the issue above, then re-run ./bootstrap.sh (finished phases skip quickly) or just this phase: ./bootstrap.sh --phase $phase"
  fi
done

[ "$DRY_RUN" -eq 1 ] && exit 0
[ -n "$ONLY_PHASE" ] && exit 0

cat <<EOF

==========================================================================
 Atlas instance is up. Next steps (in order):

 1. Install Claude Code and log in:
      curl -fsSL https://claude.ai/install.sh | bash
      claude
    (check https://docs.claude.com if the install command has changed)

 2. Reach it from outside this machine — from a Claude Code session on this
    box, follow docs/kb/Start Here.md and RECOVERY.md. The usual shape:
    your own Cloudflare account + a cloudflared tunnel pointing at
    http://127.0.0.1:${PORT}, then connect Claude's web/phone/desktop
    connectors to your public URL. Once the real URL exists:
      ./bootstrap.sh --phase 50 --issuer https://your-real-url --principal ${PRINCIPAL}

 3. Connector login uses the OAuth login secret printed ONCE by phase 40
    above (rotate with recovery/reset-login-secret.sh if lost).
==========================================================================
EOF
