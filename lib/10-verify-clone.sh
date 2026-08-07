#!/usr/bin/env bash
# lib/10-verify-clone.sh — sanity-check that this directory is a healthy git
# clone of the engine repo, and prepare it for local use.
#
# The clone itself IS the engine lay-down: nothing is copied onto this box by
# anything else. This phase only protects against "downloaded a zip instead
# of cloning" or a truncated checkout, then:
#   - sets a repo-local git identity if none is usable (the store commits
#     entity changes locally, which needs one),
#   - sets pull.rebase=false so a future `git pull` upgrade merges cleanly
#     over local store commits instead of erroring on divergent branches.
#
# Usage: ./lib/10-verify-clone.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

banner "Verifying the clone at $REPO_ROOT"

[ -d "$REPO_ROOT/.git" ] || die "no .git at $REPO_ROOT — this must be a git clone of the engine repo, not an unpacked archive. Re-fetch with: git clone <repo-url> $REPO_ROOT"
for f in VERSION requirements.txt tools/mcp_server.py tools/store.py bootstrap.sh; do
  [ -f "$REPO_ROOT/$f" ] || die "expected file missing from the clone: $f — the checkout looks incomplete"
done
[ -d "$REPO_ROOT/seed/rules" ] || die "seed/rules/ missing from the clone — the checkout looks incomplete"

cd "$REPO_ROOT"

if ! git -C "$REPO_ROOT" config user.email >/dev/null 2>&1 \
   && ! git config --global user.email >/dev/null 2>&1; then
  git config user.name "atlas-mcp"
  git config user.email "atlas-mcp@atlas-instance.local"
  log "set repo-local git identity (atlas-mcp) — the store commits entity changes locally"
else
  log "usable git identity already present"
fi

git config pull.rebase false
log "pull.rebase=false set — future 'git pull' upgrades merge over local store commits"

# A pull that merges over local store commits would otherwise drop the owner
# into an editor for the merge message — not something a non-technical owner
# should ever see (observed live on the 2026-08-07 tc2 round).
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git config "branch.${BRANCH}.mergeoptions" "--no-edit"
log "branch.${BRANCH}.mergeoptions=--no-edit set — upgrade merges never open an editor"

log "clone verified: $(head -n1 VERSION)"
