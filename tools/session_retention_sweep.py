#!/usr/bin/env python3
"""session_retention_sweep.py — enforce per-session retention_days.

Each session entity declares retention_days (default 30); until now
nothing enforced it. Nightly sweep:

  1. active sessions older than their retention_days -> archived
  2. archived sessions whose transcript can be pruned (schema requires
     summary + tool_calls + entities_touched to survive) -> pruned,
     transcript replaced by its sha256

Edits YAML directly and makes ONE git commit for the whole sweep —
deliberately not the MCP write path, which commits per entity and
already has lock contention. Every modified session is validated
against schemas.session.Session before anything is written.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = REPO_ROOT / "entities" / "sessions"
DEFAULT_RETENTION_DAYS = 30

sys.path.insert(0, str(REPO_ROOT))
from schemas.session import Session  # noqa: E402
from tools.atlas_lock import atlas_write_lock  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _append_note(data: dict, note: str) -> None:
    prior = str(data.get("notes") or "").strip()
    data["notes"] = f"{prior}\n{note}".strip()


def sweep(dry_run: bool) -> int:
    now = _now()
    archived = pruned = 0
    changed_files: list[Path] = []

    for path in sorted(SESSIONS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        lifecycle = str(data.get("lifecycle", "active"))
        if lifecycle == "pruned":
            continue

        ts = _parse_ts(data.get("timestamp")) or _parse_ts(data.get("created_at"))
        if ts is None:
            print(f"[skip] {path.name}: unparseable timestamp", file=sys.stderr)
            continue

        retention_days = data.get("retention_days") or DEFAULT_RETENTION_DAYS
        age_days = (now - ts).days
        if age_days <= int(retention_days):
            continue

        modified = False
        if lifecycle == "active":
            data["lifecycle"] = "archived"
            data["archived_at"] = _iso(now)
            data.pop("pruned_at", None)
            _append_note(data, f"archive_reason=retention-sweep ({age_days}d > {retention_days}d)")
            archived += 1
            modified = True

        # Prune the transcript if the schema's audit-metadata floor allows it.
        transcript = str(data.get("transcript") or "")
        if (
            transcript
            and data.get("summary")
            and data.get("tool_calls")
            and data.get("entities_touched")
        ):
            data["transcript_sha256"] = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            data["transcript"] = ""
            data["lifecycle"] = "pruned"
            data["pruned_at"] = _iso(now)
            data["prune_reason"] = "retention-sweep"
            pruned += 1
            modified = True

        if not modified:
            continue

        data["updated_at"] = _iso(now)
        Session(**data)  # raises on schema violation — nothing written

        if not dry_run:
            path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
                encoding="utf-8",
            )
        changed_files.append(path)

    print(f"sweep: {archived} archived, {pruned} pruned, {len(changed_files)} files{' (dry-run)' if dry_run else ''}")

    if dry_run or not changed_files:
        return 0

    rel = [str(p.relative_to(REPO_ROOT)) for p in changed_files]
    # Serialize against the MCP server + drift remediation (shared atlas-store tree).
    with atlas_write_lock(REPO_ROOT):
        subprocess.run(["git", "-C", str(REPO_ROOT), "add", *rel], check=True)
        msg = f"sessions: retention sweep — {archived} archived, {pruned} pruned"
        commit = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "commit", "-m", msg, "--", *rel],
            capture_output=True,
            text=True,
        )
    if commit.returncode != 0:
        print(f"git commit failed: {commit.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    args = parser.parse_args()
    sys.exit(sweep(args.dry_run))
