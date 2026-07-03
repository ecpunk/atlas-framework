from __future__ import annotations

from datetime import datetime, timezone

from schemas.project import Project
from schemas.vocabulary import Vocabulary

NAME = "project_index_table"
INPUTS = ["project:*", "vocabulary:lifecycle_categories", "vocabulary:project_statuses"]
OUTPUTS = [
    "outputs/kb/Project Index.md"
]

_MAX_SUMMARY_LEN = 160


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line_summary(summary: str) -> str:
    flattened = " ".join(summary.split())
    # Prefer the first sentence; fall back to a hard truncation.
    first_sentence_end = flattened.find(". ")
    if 0 < first_sentence_end <= _MAX_SUMMARY_LEN:
        return flattened[: first_sentence_end + 1]
    if len(flattened) <= _MAX_SUMMARY_LEN:
        return flattened
    return flattened[: _MAX_SUMMARY_LEN - 1].rstrip() + "…"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|")


def generate(store: dict) -> dict[str, str]:
    vocab_store = store.get("vocabulary", {})
    lifecycle = vocab_store.get("lifecycle_categories")
    statuses = vocab_store.get("project_statuses")
    if not isinstance(lifecycle, Vocabulary):
        raise ValueError("Missing vocabulary:lifecycle_categories in store")
    if not isinstance(statuses, Vocabulary):
        raise ValueError("Missing vocabulary:project_statuses in store")

    category_names = {value.id: value.name for value in lifecycle.values}
    status_names = {value.id: value.name for value in statuses.values}
    category_order = {value.id: index for index, value in enumerate(lifecycle.values)}

    project_store = store.get("project", {})
    projects = [item for item in project_store.values() if isinstance(item, Project)]

    def sort_key(project: Project):
        return (
            category_order.get(project.category.value_id, len(category_order)),
            project.name.lower(),
        )

    projects.sort(key=sort_key)

    lines: list[str] = [
        "# Project Index",
        "",
        "**GENERATED — do not hand-edit.** Source: atlas-store pipeline generator "
        "`project_index_table`, reading directly from the Atlas project entity "
        "store (`list_projects` / `get_project`). Regenerates on every atlas-store "
        "commit. Atlas is canonical for project status — this table is a view, "
        "not a place to edit.",
        "",
        f"Generated at: {_now_iso()}",
        "",
        "For full project detail (key decisions, phases, open items, concept doc "
        "links), see the Atlas project entity or "
        "`Projects/Atlas/40-OUTPUT/Project Index (generated).md`.",
        "",
        "| ID | Name | Category | Status | Summary |",
        "|---|---|---|---|---|",
    ]

    for project in projects:
        category_label = category_names.get(project.category.value_id, project.category.value_id)
        status_label = status_names.get(project.status.value_id, project.status.value_id)
        summary = _escape_cell(_one_line_summary(project.summary))
        name = _escape_cell(project.name)
        lines.append(f"| `{project.id}` | {name} | {category_label} | {status_label} | {summary} |")

    lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    return {OUTPUTS[0]: content}
