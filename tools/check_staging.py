#!/usr/bin/env python3
"""check_staging.py — guard that staging/ holds only unreviewed proposals.

staging/ is a transient operator-review queue: atlas-drift writes stub YAMLs there
(remediate.py, propose tier) and they are meant to disappear once promoted into
entities/ (handled in mcp_server._write_and_commit). A stub whose id ALREADY exists
under entities/ is therefore stale — either an already-promoted leftover or an
obsolete proposal. A 2026-06-22 audit found 185 orphaned session stubs + 9 stale
entity stubs accumulated this way; this guard fails fast if it recurs.

Exit 0 = clean. Exit 1 = stale stub(s) found (printed). Safe to wire into the
drift loop or run ad hoc.

Usage:
    python tools/check_staging.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = REPO_ROOT / "staging"
ENTITIES_DIR = REPO_ROOT / "entities"


def find_stale_stubs() -> list[tuple[str, str]]:
    """Return (stub_filename, matching_entity_relpath) for each promoted/stale stub."""
    stale: list[tuple[str, str]] = []
    if not STAGING_DIR.is_dir():
        return stale
    for stub in sorted(STAGING_DIR.glob("*.yaml")):
        # promotion maps staging/<id>.yaml -> entities/<type>/<id>.yaml
        matches = sorted(ENTITIES_DIR.glob(f"*/{stub.stem}.yaml"))
        for match in matches:
            stale.append((stub.name, str(match.relative_to(REPO_ROOT))))
    return stale


def main() -> int:
    stale = find_stale_stubs()
    if not stale:
        print("staging/ clean — no stub shadows an existing entity")
        return 0
    print(f"STALE STAGING STUBS ({len(stale)}): each has a promoted/existing entity counterpart")
    for stub_name, entity_path in stale:
        print(f"  staging/{stub_name}  ->  {entity_path}")
    print("\nThese should have been unlinked at promotion. Remove them from staging/.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
