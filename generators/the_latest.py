from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from schemas.project import Project
from schemas.task import Task

NAME = "the_latest"
INPUTS = [
    "project:*",
    "task:*",
    "outputs/Project State Deltas.md",
    "outputs/project_state_deltas.json",
    ". (git log)",
    ". (git log)",
    "outputs/state/executive/brief-*.md",
]
OUTPUTS = [
    "outputs/kb/The Latest.md"
]

_SERVICES_REPO = Path(".")
_ATLAS_STORE_REPO = Path(".")
_DELTAS_JSON = Path(
    "outputs/project_state_deltas.json"
)
_EXECUTIVE_BRIEF_DIR = Path("outputs/state/executive")

_LOOKBACK_DAYS = 14
_MAX_COMMITS_PER_REPO = 25
_MAX_HIGH_PRIORITY_TASKS = 15
_MAX_SIGNALS_SHOWN = 8

# Mechanical/regeneration commit subjects that are noise for a "what happened
# lately" narrative view — they fire on every pipeline run or curation pass and
# carry no decision content. Exact-match or prefix-match, checked case-sensitively
# against the observed commit message conventions in both repos.
_NOISE_EXACT = {
    "atlas: regenerate outputs",
    "kb-curation: regenerate report",
}
_NOISE_PREFIXES = (
    "drift: auto-delete",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_noise(subject: str) -> bool:
    if subject in _NOISE_EXACT:
        return True
    return any(subject.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _recent_commits(repo: Path) -> list[tuple[str, str]]:
    """Return (date, subject) pairs from the last _LOOKBACK_DAYS, newest first,
    noise-filtered and de-duplicated by subject. Silent-on-failure: a missing
    repo or git error yields an empty list rather than breaking the pipeline."""
    if not repo.is_dir():
        return []
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo), "log",
                f"--since={_LOOKBACK_DAYS} days ago",
                "--pretty=format:%ad|%s",
                "--date=short",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    seen_subjects: set[str] = set()
    commits: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        date, subject = line.split("|", 1)
        subject = subject.strip()
        if not subject or _is_noise(subject):
            continue
        if subject in seen_subjects:
            continue
        seen_subjects.add(subject)
        commits.append((date, subject))

    return commits[:_MAX_COMMITS_PER_REPO]


def _latest_brief_pointer() -> str:
    if not _EXECUTIVE_BRIEF_DIR.is_dir():
        return "_No executive brief directory found._"
    briefs = sorted(_EXECUTIVE_BRIEF_DIR.glob("brief-*.md"))
    if not briefs:
        return "_No executive brief generated yet._"
    latest = briefs[-1]
    return (
        f"Latest: `{latest}` (nightly executive-organ output — gitignored runtime "
        "state, not version-controlled; read directly on the host)."
    )


def _load_state_deltas() -> dict:
    if not _DELTAS_JSON.exists():
        return {}
    try:
        return json.loads(_DELTAS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _render_commit_section(title: str, commits: list[tuple[str, str]]) -> list[str]:
    lines = [f"### {title}", ""]
    if not commits:
        lines.append("_No non-mechanical commits in the last 14 days._")
    else:
        for date, subject in commits:
            lines.append(f"- {date} — {subject}")
    lines.append("")
    return lines


def generate(store: dict) -> dict[str, str]:
    project_store = store.get("project", {})
    task_store = store.get("task", {})

    projects = [item for item in project_store.values() if isinstance(item, Project)]
    tasks = [item for item in task_store.values() if isinstance(item, Task)]

    open_states = {"open", "in_progress", "blocked"}
    high_priorities = {"critical", "high"}
    high_priority_open = [
        task for task in tasks
        if task.status in open_states and task.priority in high_priorities
    ]
    high_priority_open.sort(key=lambda item: item.updated_at, reverse=True)

    project_names = {project.id: project.name for project in projects}

    services_commits = _recent_commits(_SERVICES_REPO)
    atlas_store_commits = _recent_commits(_ATLAS_STORE_REPO)

    deltas = _load_state_deltas()

    lines: list[str] = [
        "# The Latest",
        "",
        "**GENERATED — do not hand-edit.** Source: atlas-store pipeline generator "
        "`the_latest` (Atlas project/task entities, git history of both repos, the "
        "`project_state_deltas` generator, and the executive organ's latest brief). "
        "Regenerates on every atlas-store commit.",
        "",
        f"Generated at: {_now_iso()}",
        "",
        "This is a short dated view of recent stack activity, not an archive. For "
        "the append-only past-tense record, see `System History.md`. For full "
        "project detail, see the Atlas project entities (`list_projects` / "
        "`get_project`) or `Projects/Atlas/40-OUTPUT/Project Index (generated).md`.",
        "",
        "---",
        "",
        f"## Recent commits (last {_LOOKBACK_DAYS} days, non-mechanical)",
        "",
    ]

    lines.extend(_render_commit_section("services repo", services_commits))
    lines.extend(_render_commit_section("atlas-store repo", atlas_store_commits))

    lines.append("---")
    lines.append("")
    lines.append("## Project state deltas")
    lines.append("")
    if deltas:
        signal_count = deltas.get("signal_count", 0)
        create_n = len(deltas.get("create", []))
        update_n = len(deltas.get("update", []))
        close_n = len(deltas.get("close", []))
        lines.append(
            f"As of {deltas.get('generated_at', 'unknown')}: {signal_count} active "
            f"signal(s) — create={create_n}, update={update_n}, close={close_n}."
        )
        lines.append("")
        signals = deltas.get("signals", [])
        if signals:
            lines.append("Top signals:")
            for signal in signals[:_MAX_SIGNALS_SHOWN]:
                lines.append(f"- `{signal.get('signal_id', '?')}` — {signal.get('description', '')}")
            if len(signals) > _MAX_SIGNALS_SHOWN:
                lines.append(f"- _...and {len(signals) - _MAX_SIGNALS_SHOWN} more._")
        lines.append("")
        lines.append(
            "Full report: `Projects/Atlas/40-OUTPUT/Project State Deltas.md`."
        )
    else:
        lines.append("_No project_state_deltas.json found yet — run the pipeline once to populate it._")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Open high-priority tasks")
    lines.append("")
    lines.append(f"Count (open/in_progress/blocked, priority critical or high): {len(high_priority_open)}")
    lines.append("")
    if high_priority_open:
        for task in high_priority_open[:_MAX_HIGH_PRIORITY_TASKS]:
            project_name = project_names.get(task.project_id, task.project_id)
            lines.append(
                f"- [{task.status}/{task.priority}] {task.title} — {project_name} (`{task.id}`)"
            )
        if len(high_priority_open) > _MAX_HIGH_PRIORITY_TASKS:
            lines.append(f"- _...and {len(high_priority_open) - _MAX_HIGH_PRIORITY_TASKS} more._")
        lines.append("")
    lines.append(
        "Full list: `Projects/Atlas/40-OUTPUT/Open Tasks Rollup (generated).md`."
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Executive brief")
    lines.append("")
    lines.append(_latest_brief_pointer())
    lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    return {OUTPUTS[0]: content}
