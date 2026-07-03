"""Commit regenerated Atlas outputs into the services repo — race-safe.

Called by atlas-store's post-commit hook after tools/pipeline.py --write.
Replaces the hook's inline `git add <dir> && ... git commit` (no pathspec on the
commit), which captured the entire staged index and swept a concurrent agent's
staged files into services commit a3309c7a on 2026-07-02.

Two-part fix, same pattern as mcp_server._git_commit_paths:
  1. atlas_write_lock scoped to the SERVICES repo serializes against every
     other services-repo committer, and
  2. add, the emptiness check, and the commit all carry an explicit pathspec,
     so this commit can only ever contain generated-output files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atlas_lock import atlas_write_lock  # noqa: E402

# env override exists for the race regression test only
SERVICES_REPO = Path(os.environ.get("ATLAS_SERVICES_REPO", "."))
# every path the pipeline generates into the services repo — root-level generated
# views (The Latest, Project Index) included, or they rot as uncommitted drift
OUTPUTS_PATHSPECS = [
    "docs/kb/Projects/Atlas/40-OUTPUT/",
    "docs/kb/The Latest.md",
    "docs/kb/Project Index.md",
]
MESSAGE = "atlas: regenerate outputs"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(SERVICES_REPO), *args],
        capture_output=True,
        text=True,
    )


def main() -> int:
    with atlas_write_lock(SERVICES_REPO):
        add = _git("add", "--", *OUTPUTS_PATHSPECS)
        if add.returncode != 0:
            print(f"add failed: {add.stderr.strip()}", file=sys.stderr)
            return add.returncode
        diff = _git("diff", "--cached", "--quiet", "--", *OUTPUTS_PATHSPECS)
        if diff.returncode == 0:
            return 0  # no output changes staged; leave everything else alone
        commit = _git("commit", "-m", MESSAGE, "--", *OUTPUTS_PATHSPECS)
        if commit.returncode != 0:
            print(f"commit failed: {commit.stderr.strip()}", file=sys.stderr)
            return commit.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
