#!/usr/bin/env python3
"""install_local.py — set this clone up as a local Atlas for Claude Code.

Works on Windows, macOS and Linux. Needs Python 3.10+ and git; nothing is
installed system-wide and no network service is started. Claude Code launches
tools/local_server.py itself whenever a session needs Atlas.

Steps (each safe to re-run):
  1. check Python and git
  2. build .venv/ inside the clone and install requirements.txt into it
  3. seed the store: the starter rules into entities/rules/ (first run only)
  4. lock the clone so its data can never be pushed: push URL disabled on
     every remote, a refusing pre-push hook, commit signing off for this clone
  5. self-test: start the server over stdio and call stack_summary
  6. register the server with Claude Code for your user (`claude mcp add`)

Usage:
  python install_local.py [--name atlas] [--exclude PATTERNS] [--no-claude]

  --exclude   comma-separated tool-name patterns to leave out of the server
              (default: the estate-admin surfaces a documentation instance
              does not use; pass --exclude "" to keep every tool)
  --no-claude skip step 6 and print the command instead
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SERVER = ROOT / "tools" / "local_server.py"
MARKER = ROOT / ".ai-bootstrap-version"
ENTITY_DIRS = ["agents", "consumer_profiles", "memory", "projects", "publications", "rules",
               "runtimes", "servers", "services", "sessions", "skills", "tasks", "trail"]
DEFAULT_EXCLUDE = "*publication*,*consumer_profile*,*runtime*,*trail*"
NO_PUSH_URL = "no-push--this-clone-holds-local-atlas-data"
GIT_ID = {"GIT_AUTHOR_NAME": "atlas-mcp", "GIT_COMMITTER_NAME": "atlas-mcp",
          "GIT_AUTHOR_EMAIL": "atlas-mcp@atlas-instance.local",
          "GIT_COMMITTER_EMAIL": "atlas-mcp@atlas-instance.local"}
PRE_PUSH_HOOK = """#!/bin/sh
echo "push refused: this clone holds local Atlas data and is never pushed." >&2
echo "(installed by install_local.py; get engine updates with: git pull)" >&2
exit 1
"""


def step(n: int, msg: str) -> None:
    print(f"\n[{n}/6] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def check_prereqs() -> None:
    step(1, "Checking Python and git")
    if sys.version_info < (3, 10):
        die(f"Python 3.10 or newer is required (this is {sys.version.split()[0]})")
    if not shutil.which("git"):
        die("git is not on PATH; install git and re-run")
    if not SERVER.is_file() or not (ROOT / "tools" / "mcp_server.py").is_file():
        die(f"{ROOT} does not look like an Atlas clone (tools/ is incomplete)")
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        die(f"{ROOT} is not a git clone; clone the repo rather than downloading a zip")
    print(f"  Python {sys.version.split()[0]}, {shutil.which('git')}")


def build_venv() -> None:
    step(2, "Building .venv and installing requirements")
    if not VENV_PY.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    print(f"  {VENV_PY}")


def seed_store() -> None:
    step(3, "Seeding the entity store")
    for d in ENTITY_DIRS:
        p = ROOT / "entities" / d
        p.mkdir(parents=True, exist_ok=True)
        (p / ".gitkeep").touch(exist_ok=True)
    (ROOT / "state").mkdir(exist_ok=True)
    if MARKER.exists():
        print("  already seeded; entities/ left as they are")
        return
    seeds = sorted((ROOT / "seed" / "rules").glob("*.yaml"))
    if not seeds:
        die("no seed/rules/*.yaml in the clone")
    for f in seeds:
        shutil.copyfile(f, ROOT / "entities" / "rules" / f.name)
    version = ROOT / "VERSION"
    MARKER.write_text(version.read_text(encoding="utf-8") if version.exists() else "seeded\n",
                      encoding="utf-8")
    git("add", "entities", MARKER.name)
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "atlas-instance: seed entities/rules/",
            env=GIT_ID)
    print(f"  {len(seeds)} starter rules")


def lock_clone() -> None:
    step(4, "Locking the clone so its data is never pushed")
    remotes = [r for r in git("remote").stdout.split() if r]
    for r in remotes:
        git("remote", "set-url", "--push", r, NO_PUSH_URL)
    hooks = Path(git("rev-parse", "--git-path", "hooks").stdout.strip())
    hooks = hooks if hooks.is_absolute() else ROOT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    git("config", "commit.gpgsign", "false")
    print(f"  push disabled on: {', '.join(remotes) or '(no remotes)'}; pre-push hook refuses; "
          "commit signing off for this clone")


SELF_TEST = r"""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            res = await s.call_tool("stack_summary", {})
            print(json.dumps({"tools": len(tools), "summary": res.content[0].text}))
asyncio.run(main())
"""


def self_test(exclude: str) -> int:
    step(5, "Self-test: starting the server over stdio")
    env = {**os.environ, "ATLAS_TOOLS_EXCLUDE": exclude}
    proc = subprocess.run([str(VENV_PY), "-c", SELF_TEST, str(SERVER)], capture_output=True,
                          text=True, env=env, timeout=120)
    if proc.returncode != 0:
        die(f"self-test failed:\n{proc.stderr.strip()[-2000:]}")
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"  {out['tools']} tools; stack_summary: {out['summary']}")
    return out["tools"]


def register(name: str, exclude: str, skip: bool) -> None:
    step(6, "Registering with Claude Code")
    cmd = ["claude", "mcp", "add", "-s", "user", name]
    if exclude:
        cmd += ["-e", f"ATLAS_TOOLS_EXCLUDE={exclude}"]
    cmd += ["--", str(VENV_PY), str(SERVER)]
    shown = " ".join(f'"{c}"' if (" " in c or "*" in c) else c for c in cmd)
    claude = shutil.which("claude")
    if skip or not claude:
        why = "--no-claude given" if skip else "the claude command is not on PATH"
        print(f"  skipped ({why}). Run this once Claude Code is installed:\n    {shown}")
        return
    cmd[0] = claude
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"claude mcp add failed: {proc.stderr.strip() or proc.stdout.strip()}\n"
            f"  If an '{name}' server already exists: claude mcp remove {name} -s user, then re-run.")
    print(f"  registered '{name}' for your user; every new Claude Code session can use it")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default="atlas")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE)
    ap.add_argument("--no-claude", action="store_true")
    a = ap.parse_args()
    check_prereqs()
    build_venv()
    seed_store()
    lock_clone()
    self_test(a.exclude)
    register(a.name, a.exclude, a.no_claude)
    print("\nDone. In a new Claude Code session, ask: get_kb_doc(\"Start Here.md\") on the "
          f"'{a.name}' MCP server.")


if __name__ == "__main__":
    main()
