"""OKF frontmatter for KB docs: who wrote a doc and when.

Convention: the OKF spec, https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md

The KB write tools call stamp() so every write records `generated: {by, at}`
and a session can never record a person as the author. A backfill script
can use the same helpers.

Frontmatter is edited as text, not round-tripped through a YAML dump, so a
hand-written block keeps its layout and its timestamps keep their format.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import yaml

LEGACY_ACTOR = "claude/unknown"
_ACTOR_RE = re.compile(
    r"^(?:process:[a-z0-9][a-z0-9_.-]*|[a-z0-9][a-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.\[\]-]*)$"
)
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):")

# Files whose top lines belong to another reader (the skill loader), or that a
# generator rewrites whole on every run (a block added there would be dropped).
EXEMPT_NAMES = {"SKILL.md", "Project Index.md", "The Latest.md"}
EXEMPT_PREFIXES = (
    "Projects/Atlas/40-OUTPUT/",
)


def is_exempt(rel_path: str) -> bool:
    p = PurePosixPath(rel_path)
    return (p.name in EXEMPT_NAMES or "(generated)" in p.name
            or p.as_posix().startswith(EXEMPT_PREFIXES))


def is_copied_source(rel_path: str) -> bool:
    """Third-party material saved for citation (a `NN-SOURCE` folder). Nobody
    here wrote it, so the backfill does not label it as Claude's."""
    return any(re.match(r"^\d{2}-SOURCE$", part) for part in PurePosixPath(rel_path).parts)


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split(text: str) -> tuple[str | None, dict[str, Any] | None, str]:
    """Return (raw_frontmatter, parsed, body). raw excludes the --- lines.
    A leading --- that does not open a YAML mapping (a horizontal rule) is
    not frontmatter: returns (None, None, text)."""
    if not text.startswith("---\n"):
        return None, None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            raw = "\n".join(lines[1:i])
            try:
                meta = yaml.safe_load(raw)
            except yaml.YAMLError:
                return None, None, text
            if not isinstance(meta, dict):
                return None, None, text
            return raw, meta, "\n".join(lines[i + 1:]).lstrip("\n")
    return None, None, text


def join(raw: str, body: str) -> str:
    return f"---\n{raw.strip(chr(10))}\n---\n\n{body.lstrip(chr(10))}"


def _drop_key(raw: str, key: str) -> str:
    """Remove one top-level key and its indented block from raw frontmatter."""
    out: list[str] = []
    skipping = False
    for line in raw.split("\n"):
        m = _TOP_KEY_RE.match(line)
        if m:
            skipping = m.group(1) == key
        elif skipping and line.strip() and not line[:1].isspace():
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).strip("\n")


def _set_generated(raw: str, actor: str, at: str) -> str:
    base = _drop_key(raw, "generated")
    block = f"generated:\n  by: {actor}\n  at: {at}"
    return f"{base}\n{block}" if base else block


def validate_actor(actor: str, operation: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise ValueError(
            f"Refusing {operation}: `actor` is required. Pass who is writing, as "
            "'<tool>/<model>' (for example 'claude-code/claude-fable-5-2') or "
            "'process:<id>' for automation. The tool records it as generated.by."
        )
    if actor.lower().startswith("human:"):
        raise ValueError(
            f"Refusing {operation}: a session cannot record a person as the author "
            f"({actor!r}). Pass your own actor. A person's words go in the body as a "
            "dated verbatim quote cited to a human: source."
        )
    if not _ACTOR_RE.match(actor):
        raise ValueError(
            f"Refusing {operation}: actor {actor!r} is not '<tool>/<model>' or 'process:<id>'."
        )
    return actor


def reject_human_claims(meta: dict[str, Any] | None, operation: str) -> None:
    """A session may not write a person into generated or verified."""
    if not meta:
        return
    claims: list[str] = []
    gen = meta.get("generated")
    if isinstance(gen, dict):
        claims.append(str(gen.get("by", "")))
    ver = meta.get("verified")
    for entry in ver if isinstance(ver, list) else [ver]:
        if isinstance(entry, dict):
            claims.append(str(entry.get("by", "")))
    for by in claims:
        if by.strip().lower().startswith("human:"):
            raise ValueError(
                f"Refusing {operation}: the frontmatter names {by!r} in generated or "
                "verified. A session cannot record that a person wrote or checked a doc."
            )


# Folders whose docs are all one kind, whatever the file is called.
FOLDER_TYPES = {
    # "<folder name>": "<doc type>",
}


def infer_type(rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    for part in p.parts[:-1]:
        if part in FOLDER_TYPES:
            return FOLDER_TYPES[part]
    name = p.stem.upper().replace("-", "_").replace(" ", "_")
    stage = next((part for part in p.parts if re.match(r"^\d{2}-", part)), "")
    stage = stage.split("-", 1)[1].upper() if stage else ""
    words = set(name.split("_"))
    if "DECISION" in words or "DECISIONS" in words or name.startswith("DECISION"):
        return "Decision"
    if words & {"LESSON", "LESSONS"} or stage == "LESSONS":
        return "Lesson"
    if words & {"RUNBOOK", "RECREATE", "PLAYBOOK"}:
        return "Runbook"
    if words & {"LOG", "CHANGELOG", "HISTORY", "JOURNAL"}:
        return "Log"
    if words & {"PLAN", "ROADMAP"} or stage == "PLAN":
        return "Plan"
    if words & {"RESEARCH", "FINDINGS", "SURVEY"} or stage == "RESEARCH":
        return "Research"
    if "CONCEPT" in words:
        return "Concept"
    if words & {"DESIGN", "SPEC", "ARCHITECTURE"}:
        return "Design"
    if words & {"README", "INDEX", "REFERENCE", "GLOSSARY"}:
        return "Reference"
    if stage == "CONCEPT":
        return "Concept"
    if stage == "DESIGN":
        return "Design"
    return "Note"


_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def infer_title(rel_path: str, body: str) -> str:
    m = _H1_RE.search(body)
    title = m.group(1).strip() if m else PurePosixPath(rel_path).stem.replace("_", " ")
    return re.sub(r"[*`]", "", title).strip() or PurePosixPath(rel_path).stem


def _scalar(value: str) -> str:
    """One YAML scalar, quoted only when YAML needs it."""
    return yaml.safe_dump(value, allow_unicode=True, width=10000, default_style=None).split("\n...")[0].strip()


def legacy_block(rel_path: str, body: str, actor: str, at: str) -> str:
    """Frontmatter for a doc that predates labelling."""
    return (
        f"type: {infer_type(rel_path)}\n"
        f"title: {_scalar(infer_title(rel_path, body))}\n"
        f"legacy: true\n"
        f"generated:\n  by: {actor}\n  at: {at}"
    )


def stamp(new_text: str, existing_text: str | None, rel_path: str, actor: str,
          operation: str, doc_type: str = "", at: str = "") -> str:
    """Return new_text with frontmatter carrying generated: {by: actor, at: now}.

    existing_text is the doc as it is on disk (None on create).
    - new_text has frontmatter: it is kept; `generated` is replaced.
    - it has none: the existing doc's frontmatter is carried over.
    - neither has any: on create a `type` is required (frontmatter or doc_type);
      on an edit to an unlabelled doc a legacy block is built.
    `legacy: true` on the existing doc is never dropped. If nothing but the
    stamp would change, the existing text is returned untouched.
    """
    if is_exempt(rel_path):
        return new_text
    at = at or now_stamp()
    raw, meta, body = split(new_text)
    reject_human_claims(meta, operation)
    old_raw, old_meta, old_body = split(existing_text) if existing_text is not None else (None, None, "")

    if raw is None:
        if old_raw is not None:
            raw, meta = old_raw, old_meta
        elif existing_text is None:
            if not doc_type.strip():
                raise ValueError(
                    f"Refusing {operation}: a new KB doc needs a type. Start the content "
                    "with frontmatter holding `type:`, or pass doc_type (Concept, Decision, "
                    "Plan, Design, Research, Runbook, Lesson, Log, Reference, Note, or a "
                    "project's own)."
                )
            raw = f"type: {doc_type.strip()}\ntitle: {_scalar(infer_title(rel_path, body))}"
            meta = {"type": doc_type.strip()}
        else:
            raw = legacy_block(rel_path, body, actor, at)
            meta = {"type": infer_type(rel_path), "legacy": True}

    if not str((meta or {}).get("type") or "").strip():
        inferred = doc_type.strip() or (infer_type(rel_path) if existing_text is not None else "")
        if not inferred:
            raise ValueError(f"Refusing {operation}: the frontmatter has no `type`.")
        raw = f"type: {inferred}\n{raw}"
    if old_meta and old_meta.get("legacy") is True and not (meta or {}).get("legacy"):
        raw = f"{_drop_key(raw, 'legacy')}\nlegacy: true"

    if existing_text is not None and old_raw is not None:
        same_meta = _drop_key(raw, "generated") == _drop_key(old_raw, "generated")
        if same_meta and body.strip("\n") == old_body.strip("\n"):
            return existing_text
    return join(_set_generated(raw, actor, at), body)


def patch_body(existing_text: str, compute) -> str:
    """Apply a body-only edit (append, section replace), leaving frontmatter as is."""
    raw, _meta, body = split(existing_text)
    if raw is None:
        return compute(existing_text)
    return join(raw, compute(body))
