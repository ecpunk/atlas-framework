#!/usr/bin/env bash
# lib/30-seed-store.sh — seed entities/rules/ from the curated seed/rules/
# set shipped in this repo, then write the marker file .ai-bootstrap-version
# and commit.
#
# Guard: if the repo already has .ai-bootstrap-version, this refuses to touch
# entities/ again unless BOTH --reseed is passed AND you confirm
# interactively. Already-seeded is a SKIP, not an error.
#
# Usage: ./lib/30-seed-store.sh [--reseed]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

RESEED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reseed) RESEED=1; shift ;;
    *) die "unrecognized argument: $1" ;;
  esac
done

banner "Seeding the entity store at $REPO_ROOT"

cd "$REPO_ROOT"
MARKER=".ai-bootstrap-version"

if [ -f "$MARKER" ]; then
  if [ "$RESEED" != "1" ]; then
    log "Already seeded (marker $MARKER present) — skipping, entities/ untouched."
    log "Pass --reseed to deliberately overwrite entities/rules/*.yaml with the current seed set."
    exit 0
  fi
  echo "This store is already seeded:"
  cat "$MARKER"
  echo
  read -r -p "Re-seeding overwrites entities/rules/*.yaml with the current seed/rules/ set. Continue? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "aborted — no changes made"; exit 4 ;;
  esac
fi

[ -d seed/rules ] || die "no seed/rules/ directory found — the clone looks incomplete"

mkdir -p entities/rules
for d in agents consumer_profiles memory projects publications rules runtimes servers services sessions skills tasks trail; do
  mkdir -p "entities/$d"
  [ -e "entities/$d/.gitkeep" ] || touch "entities/$d/.gitkeep"
done

cp seed/rules/*.yaml entities/rules/
if [ -f VERSION ]; then cp VERSION "$MARKER"; else date -u +"seeded_at: %Y-%m-%dT%H:%M:%SZ" > "$MARKER"; fi

git add entities/ "$MARKER"
if git diff --cached --quiet; then
  log "no changes to commit (seed content already matches entities/rules/)"
else
  git commit -q -m "atlas-instance: seed entities/rules/ (reseed=$RESEED)"
  log "committed seed rules"
fi
