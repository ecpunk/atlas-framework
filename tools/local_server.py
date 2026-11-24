#!/usr/bin/env python3
"""local_server.py — run this Atlas instance as a local stdio MCP server.

Claude Code starts this process itself and talks to it over stdin/stdout, so
there is no network listener, no login secret and no system service to
install. Runs on Linux, macOS and Windows (Python 3.10+, git on PATH).
install_local.py registers it with Claude Code; nothing else needs to.

Everything Atlas records (entities/, docs/kb/) is committed to THIS clone's
own git history and never pushed anywhere: install_local.py disables push on
every remote of the clone.

Env (all optional; install_local.py writes them into the MCP config):
  ATLAS_TOOLS_EXCLUDE        comma-separated tool-name patterns to leave out
                             (fnmatch, e.g. "*publication*,*trail*")
  ATLAS_KB_DOC_ROOT          default <repo>/docs/kb
  ATLAS_SERVICES_REPO_ROOT   default <repo>
  ATLAS_LOCAL_HOST_ID        default "local" (the host id services point at)
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_windows_flock() -> None:
    """Windows has no fcntl. The engine only ever calls flock(fd, LOCK_EX) and
    flock(fd, LOCK_UN) on an os.open() descriptor of a dedicated lock file, so
    a byte-range lock on byte 0 gives the same mutual exclusion between
    processes (two Claude Code sessions each run their own server)."""
    import msvcrt
    import time
    import types

    shim = types.ModuleType("fcntl")
    shim.LOCK_SH, shim.LOCK_EX, shim.LOCK_NB, shim.LOCK_UN = 1, 2, 4, 8

    def flock(fd, op):
        fd = fd.fileno() if hasattr(fd, "fileno") else fd
        os.lseek(fd, 0, os.SEEK_SET)
        if op & shim.LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if op & shim.LOCK_NB:
                    raise
                time.sleep(0.05)

    shim.flock = flock
    sys.modules["fcntl"] = shim


def main() -> None:
    os.environ.setdefault("ATLAS_KB_DOC_ROOT", str(REPO_ROOT / "docs" / "kb"))
    os.environ.setdefault("ATLAS_SERVICES_REPO_ROOT", str(REPO_ROOT))
    os.environ.setdefault("ATLAS_LOCAL_HOST_ID", "local")
    # stdio has no HTTP surface, so the OAuth layer has nothing to protect.
    os.environ["ATLAS_OAUTH_ENABLED"] = "0"
    extra = REPO_ROOT / "mcp-instructions.md"
    if extra.is_file():
        os.environ.setdefault("ATLAS_MCP_INSTRUCTIONS_EXTRA_FILE", str(extra))

    if os.name == "nt":
        _install_windows_flock()
    sys.path.insert(0, str(REPO_ROOT))
    import tools.mcp_server as engine

    patterns = [p.strip() for p in os.environ.get("ATLAS_TOOLS_EXCLUDE", "").split(",") if p.strip()]
    if patterns:
        for tool in asyncio.run(engine.mcp.list_tools()):
            if any(fnmatch.fnmatchcase(tool.name, p) for p in patterns):
                engine.mcp.remove_tool(tool.name)

    engine.mcp.run("stdio")


if __name__ == "__main__":
    main()
