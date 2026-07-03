"""Race-safety test for the services-repo KB committer (_git_commit_paths).

Regression guard for the 2026-07-02 live sweep bug: a create_kb_doc committing a
doc into the SERVICES repo captured concurrently-staged unrelated files from
parallel agents (it committed the whole index instead of an explicit pathspec).

CRITICAL safety property asserted here: a concurrent agent's staged-but-uncommitted
file must remain staged and untouched after the KB commit, and the KB commit must
contain ONLY its own path.

Run: python -m pytest tests/test_git_commit_paths_race.py   (or run directly).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.mcp_server import _git_commit_paths  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")


def _run_case(tmp: Path) -> None:
    repo = tmp / "svc"
    _init_repo(repo)

    # Concurrent agent stages an unrelated file A (staged, uncommitted).
    file_a = repo / "agent_A.txt"
    file_a.write_text("agent A work in progress\n")
    _git(repo, "add", "agent_A.txt")

    # KB write path writes doc B and commits it via the helper under test.
    doc_dir = repo / "docs" / "kb"
    doc_dir.mkdir(parents=True)
    file_b = doc_dir / "B.md"
    file_b.write_text("# doc B\n")
    rel_b = str(file_b.relative_to(repo))

    result = _git_commit_paths(repo, [rel_b], "docs: create B via Atlas KB Write API")
    assert result.get("ok") and result.get("committed"), f"commit failed: {result}"

    # 1. The commit must contain ONLY B, not A.
    committed = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert committed == [rel_b], f"commit swept extra files: {committed}"

    # 2. A must still be staged (present in the index, added-not-committed).
    staged = _git(repo, "diff", "--cached", "--name-only").split()
    assert "agent_A.txt" in staged, f"agent A was disturbed; staged={staged}"

    # 3. A's content must be intact and it must not be in HEAD.
    assert (repo / "agent_A.txt").read_text() == "agent A work in progress\n"
    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "agent_A.txt" not in tree, "agent A leaked into HEAD"

    print("PASS: KB commit contained only B; concurrent staged file A remained staged and untouched.")


def test_kb_commit_does_not_sweep_concurrent_staged_files(tmp_path):
    _run_case(tmp_path)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        _run_case(Path(td))
