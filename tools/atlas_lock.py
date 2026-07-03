"""atlas_write_lock — cross-process serialization for atlas-store git writes.

The atlas-store entity store is a git working tree written by several independent
processes: the MCP server (live entity writes), drift remediation (stub proposals),
and the session-retention sweep — none of which were serialized. Two writers that
interleave `git add`/`git commit` can collide on `.git/index.lock` (spurious
failure) or, with a non-pathspec commit, capture each other's staged files.

The fix has two parts, applied together at every committer:
  1. commit with an explicit pathspec (`git commit -- <paths>`) so a commit can
     only ever include its own files, and
  2. hold this advisory lock around the add+commit so concurrent writers run one
     at a time.

The lock file lives inside `.git/` so it is never itself committed, and its path
is derived purely from the repo root so every process agrees on the same lock.

Scope note: adopters so far are mcp_server, remediate, and session_retention.
Any future atlas-store committer (e.g. consolidator, generators) must
import and use this too, or it reopens the race for itself.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


@contextlib.contextmanager
def atlas_write_lock(repo_root: Path):
    lock_path = Path(repo_root) / ".git" / "atlas-write.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
