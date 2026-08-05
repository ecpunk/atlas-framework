#!/usr/bin/env python3
"""Atlas MCP Read API — exposes the canonical entity store over streamable-http.

Port: 8105 (lan-only)
Auth: X-API-Key header
Transport: streamable-http at /mcp
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from tools.atlas_lock import atlas_write_lock
from tools.refs import build_ref_index, check_refs
from tools.store import Store, load_store, _schema_for_entity_dir

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8105
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_API_KEY_FILE = REPO_ROOT / ".secrets" / "api_key.txt"

_store: Store | None = None


def _get_store() -> Store:
    global _store
    if _store is None:
        _store = load_store(REPO_ROOT)
    return _store


def _invalidate_store() -> None:
    global _store
    _store = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _validate_domain_tags(domain_tags: list[str] | None) -> list[str] | None:
    if domain_tags is None:
        return None

    if not isinstance(domain_tags, list):
        raise ValueError("domain_tags must be a list of lowercase strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for tag in domain_tags:
        if not isinstance(tag, str):
            raise ValueError("domain_tags must be a list of lowercase strings")
        cleaned = tag.strip()
        if not cleaned:
            raise ValueError("domain_tags cannot contain empty strings")
        if cleaned != cleaned.lower():
            raise ValueError("domain_tags must be lowercase")
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)

    return normalized


_TASK_STATUS_VALUES = {"open", "in_progress", "blocked", "resolved", "deferred"}
_TASK_PRIORITY_VALUES = {"critical", "high", "medium", "low"}
_SESSION_STATUS_VALUES = {"completed", "aborted", "confirm-pending", "error"}
_SESSION_LIFECYCLE_VALUES = {"active", "archived", "pruned"}
_MEMORY_TYPE_VALUES = {"identity", "preference", "expertise", "decision", "reference"}
_MEMORY_STATUS_VALUES = {"active", "superseded"}
_TRAIL_STATUS_VALUES = {"open", "pulled", "led-somewhere", "dead"}


def _slugify_token(value: str, fallback: str = "task") -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return token or fallback


def _resolve_project_id(project: str) -> str:
    store = _get_store()
    projects = store.get("project", {})
    if project in projects:
        return project

    matches = []
    needle = project.strip().lower()
    for project_id, model in projects.items():
        payload = _model_to_dict(model)
        if str(payload.get("name", "")).strip().lower() == needle:
            matches.append(project_id)

    if not matches:
        raise ValueError(f"Project '{project}' not found by id or exact name.")
    if len(matches) > 1:
        raise ValueError(f"Project name '{project}' is ambiguous. Matching ids: {sorted(matches)}")
    return matches[0]


def _next_task_id(project_id: str, title: str, source: str, source_request_id: str) -> str:
    tasks = _get_store().get("task", {})
    if source_request_id:
        stable_seed = f"{project_id}|{source}|{source_request_id}"
        stable_suffix = hashlib.sha1(stable_seed.encode("utf-8")).hexdigest()[:10]
        candidate = f"{project_id}-task-{stable_suffix}"
        if candidate not in tasks:
            return candidate

    title_token = _slugify_token(title, fallback="task")[:24]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    candidate = f"{project_id}-{title_token}-{stamp}"
    if candidate not in tasks:
        return candidate

    nonce = 2
    while f"{candidate}-{nonce}" in tasks:
        nonce += 1
    return f"{candidate}-{nonce}"


def _next_session_id(source: str, source_request_id: str) -> str:
    sessions = _get_store().get("session", {})
    if source_request_id:
        stable_seed = f"{source}|{source_request_id}"
        stable_suffix = hashlib.sha1(stable_seed.encode("utf-8")).hexdigest()[:10]
        candidate = f"session-{stable_suffix}"
        if candidate not in sessions:
            return candidate

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random_suffix = hashlib.sha1(os.urandom(16)).hexdigest()[:8]
    candidate = f"session-{stamp}-{random_suffix}"
    if candidate not in sessions:
        return candidate

    nonce = 2
    while f"{candidate}-{nonce}" in sessions:
        nonce += 1
    return f"{candidate}-{nonce}"


def _git_commit(rel_path: str | list[str], message: str) -> dict[str, Any]:
    """Stage one or more files and commit them in REPO_ROOT. Returns {"ok": True} or {"error": ...}.

    Serialized against other atlas-store writers (drift, retention) via
    atlas_write_lock, and commits with an explicit pathspec so a concurrent
    writer's staged file can never ride along in this commit.
    """
    rel_paths = [rel_path] if isinstance(rel_path, str) else list(rel_path)
    with atlas_write_lock(REPO_ROOT):
        for path in rel_paths:
            add = subprocess.run(
                ["git", "add", path],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if add.returncode != 0:
                return {"error": f"git add failed: {add.stderr.strip() or add.stdout.strip()}"}

        commit = subprocess.run(
            ["git", "commit", "-m", message, "--", *rel_paths],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "GIT_AUTHOR_NAME": "atlas-mcp", "GIT_COMMITTER_NAME": "atlas-mcp",
                 "GIT_AUTHOR_EMAIL": "atlas-mcp@atlas-instance.local", "GIT_COMMITTER_EMAIL": "atlas-mcp@atlas-instance.local"},
        )
        if commit.returncode != 0:
            return {"error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"}
        return {"ok": True, "sha": commit.stdout.strip().split()[-1] if commit.stdout.strip() else ""}


@dataclass
class EntityCheck:
    """Outcome of pre-write validation. errors block the write; warnings ride along."""

    errors: list[str]
    warnings: list[str]


def _validate_entity(entity_dir: str, entity_id: str, data: dict[str, Any]) -> EntityCheck:
    """Validate a candidate entity dict BEFORE anything is written to disk.

    Two layers:
      1. Schema — resolve entity_dir to its pydantic model and model_validate(data).
      2. References — resolve every VocabRef against the loaded vocabularies and every
         TypedRef against the loaded entity ids (tools/refs.py, shared with validate.py).

    VocabRef misses are errors, TypedRef misses are warnings — see tools/refs.py for
    why. Returns EntityCheck([], [...]) when the entity is safe to write.
    """
    validator = _schema_for_entity_dir(entity_dir)
    if validator is None:
        return EntityCheck([f"no schema module for entities/{entity_dir}/"], [])

    try:
        model = validator(data)
    except Exception as exc:
        return EntityCheck([f"schema validation failed: {exc}"], [])

    try:
        store = _get_store()
    except Exception as exc:
        # The store itself is unloadable (a pre-existing bad entity). Don't let that
        # block an otherwise-valid write — the schema layer above already passed.
        return EntityCheck([], [f"reference check skipped — store failed to load: {exc}"])

    entity_refs, vocab_values = build_ref_index(store)
    kind = entity_dir[:-1] if entity_dir.endswith("s") else entity_dir
    errors, warnings = check_refs(
        model, entity_refs, vocab_values, self_ref=f"{kind}:{entity_id}"
    )
    return EntityCheck(errors, warnings)


def _write_and_commit(
    entity_dir: str,
    entity_id: str,
    data: dict[str, Any],
    message: str,
    *,
    skip_validation: bool = False,
) -> dict[str, Any]:
    """Validate, then write entity YAML and commit.

    Validation runs BEFORE the file is touched. It used to run after the commit, which
    meant an invalid entity was already in git and had already fired the post-commit
    regeneration hook — and because load_store() validates the *whole* store, one bad
    write made every subsequent read tool raise until a human hand-fixed the YAML. A
    failed validation is now a strict no-op: nothing written, nothing staged, tree clean.

    Promotion cleanup: if a staging stub (staging/<entity_id>.yaml) exists for this
    id, it is removed in the same commit. staging/ is a transient operator-review
    queue; once a stub is promoted into entities/ the review copy must not linger
    (a 2026-06-22 audit found dozens of orphaned already-promoted stubs). Mirrors
    remediate.py's unlink pattern; also opportunistically retires any pre-existing
    stale stub whose id is written here.
    """
    check = EntityCheck([], [])
    if not skip_validation:
        check = _validate_entity(entity_dir, entity_id, data)
        if check.errors:
            return {
                "error": "validation failed — nothing written or committed",
                "entity": f"{entity_dir}/{entity_id}",
                "errors": check.errors,
                "hint": "Call list_vocabularies() to see the legal value_ids for every vocabulary.",
            }

    rel_path = f"entities/{entity_dir}/{entity_id}.yaml"
    abs_path = REPO_ROOT / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True), encoding="utf-8")

    commit_paths = [rel_path]
    staging_abs = REPO_ROOT / "staging" / f"{entity_id}.yaml"
    if staging_abs.exists():
        staging_abs.unlink()
        commit_paths.append(f"staging/{entity_id}.yaml")
        message = f"{message} (promoted from staging stub)"

    result = _git_commit(commit_paths, message)
    _invalidate_store()
    if check.warnings:
        result["warnings"] = check.warnings
    return result


def _api_key() -> str:
    env_key = os.environ.get("ATLAS_MCP_API_KEY", "").strip()
    if env_key:
        return env_key
    key_file = Path(os.environ.get("ATLAS_MCP_API_KEY_FILE", str(DEFAULT_API_KEY_FILE)))
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


def _check_auth(provided_key: str) -> bool:
    expected = _api_key()
    if not expected:
        return True  # no key configured — open (local-only use)
    return provided_key == expected


def _model_to_dict(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    return obj


_lifecycle_logger = logging.getLogger("atlas_mcp.lifecycle")


def _now_lifecycle_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_session_lifecycle(
    event_type: str,
    *,
    correlation_id: str,
    session_id: str,
    tool_name: str,
    status_code: int,
) -> None:
    _lifecycle_logger.info(
        "mcp_lifecycle event_type=%s correlation_id=%s session_id=%s tool_name=%s ts=%s status=%s",
        event_type,
        correlation_id,
        session_id,
        tool_name,
        _now_lifecycle_ts(),
        status_code,
    )


def _extract_tool_name(body: bytes) -> str:
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""

    if not isinstance(payload, dict):
        return ""

    method = payload.get("method")
    if method != "tools/call":
        return ""

    params = payload.get("params")
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            return name
    return ""


MCP_INSTRUCTIONS = """You are connected to an Atlas canonical entity store.

Atlas is the single source of truth for stack definitions: projects, services, servers, agents, rules, and vocabularies.

Read tools:
- get_project(id): full project entity
- list_projects(category?, status?): filtered project list
- get_service(id): full service entity
- list_services(): all services with key fields
- get_server(id): server entity
- get_vocabulary(id): vocabulary with all values
- get_rule(id) / list_rules(scope?, severity?): rule entities
- get_publication(id) / list_publications(): published repos/docs + their publication contract (drift-checked)
- stack_summary(): entity counts — use this for orientation

Bridge / output tools:
- get_kb_doc(name): read a knowledge base doc from the configured KB doc root (e.g. "Start Here.md")
- create_kb_doc(path, content, confirm?, commit?): create docs/kb markdown via Atlas (single sanctioned write path; degenerate payloads — empty, unexpanded $(...) substitutions, __PLACEHOLDER__ tokens — are refused)
- update_kb_doc(path, content, confirm?, commit?, allow_shrink?): update docs/kb markdown via Atlas (single sanctioned write path; FULL-FILE overwrite — degenerate payloads are refused, and content far smaller than the existing doc is refused unless allow_shrink=True)
- append_kb_doc(path, content, expected_hash?, confirm?, commit?): append text to a KB doc server-side (no full-file round-trip; truncation-safe)
- replace_kb_section(path, anchor, content, create_missing?, expected_hash?, confirm?, commit?, allow_whole_file?): replace one markdown section's body by heading anchor, leaving the rest intact (H1 anchors that span the whole doc are refused unless allow_whole_file=True)
- check_drift(service_id?, force?): reality probes — reads cached result by default; force=True runs live

Write tools (propose-confirm pattern — preview first, then confirm=True to apply):
- add_project(id, name, summary, category, status, concept_doc, gdrive_folder?, domain_tags?): add new project (autonomous)
- update_project(id, confirm?, status?, category?, summary?, domain_tags?, ...): update project fields
- add_service(id, name, summary, service_type, lifecycle, deployment_path, ...): add new service (autonomous)
- update_service(id, confirm?, lifecycle?, port?, ...): update service fields
- retire_service(id, confirm?): set service lifecycle to retired
- set_maintenance(id, hours, reason, confirm?): declare a maintenance window (until = now+hours); while active, probes pass and restart-lock lifts for this service (Gate 1.1 — state-derived authority)
- clear_maintenance(id, confirm?): end a declared maintenance window early
- add_task(project, title, next_action, closure_test, ...): add canonical task (autonomous)
- update_task(id, confirm?, status?, priority?, ...): update task fields
- talos_queue_build(goal, repo, closure_test, constraints?, title?): queue an autonomous build for Talos (dictate-to-build watcher on project 'talos'); autonomous
- talos_status / talos_list_builds / talos_cancel / talos_requeue: inspect and manage Talos build lifecycle (status+outcome, list, cancel a queued build, re-queue a finished/cancelled one)

Task query tools:
- get_task(id): read one task
- list_tasks(project?, status?, priority?, limit?): list tasks with optional filters
- list_actionable_tasks(project?, limit?): list only open/in_progress tasks (agent-facing default)
- list_open_tasks(project?, limit?): list only open/in_progress/blocked tasks (operator-facing visibility)

Session query tools:
- get_session(id): read one session
- list_sessions(source?, status?, lifecycle?, user_id?, project_id?, limit?): list sessions with filters

Memory query tools:
- list_memories(memory_type?, status?, limit?): list consolidated memory entities, optionally filtered by memory_type (identity|preference|expertise|decision|reference) and status (active|superseded)
- get_memory(id): read one consolidated memory entity in full

Use stack_summary() first when you need an overview of what's in the store.
"""


def _resolve_mcp_instructions() -> str:
    """Base MCP_INSTRUCTIONS plus optional per-instance routing prose appended
    from ATLAS_MCP_INSTRUCTIONS_EXTRA_FILE. Unset/missing/empty file = base only."""
    base = MCP_INSTRUCTIONS
    extra_path = os.environ.get("ATLAS_MCP_INSTRUCTIONS_EXTRA_FILE", "")
    if extra_path:
        p = Path(extra_path)
        if p.is_file():
            extra = p.read_text(encoding="utf-8").strip()
            if extra:
                return f"{base}\n\n{extra}"
    return base


# --- OAuth 2.1 (self-hosted authorization server) — env-gated, default OFF ---
# When ATLAS_OAUTH_ENABLED is set, atlas-mcp becomes its own OAuth AS + resource
# server (see tools/atlas_oauth.py). Until then this block is inert and the server
# behaves exactly as before (X-API-Key + localhost bypass).
_OAUTH_ENABLED = os.environ.get("ATLAS_OAUTH_ENABLED", "").lower() in ("1", "true", "yes")
_atlas_oauth_provider = None
_atlas_auth_kwargs: dict[str, Any] = {}
if _OAUTH_ENABLED:
    from atlas_oauth import AtlasOAuthProvider
    from atlas_oauth import ISSUER_URL as _OAUTH_ISSUER
    from atlas_oauth import RESOURCE_URL as _OAUTH_RESOURCE
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )

    _atlas_oauth_provider = AtlasOAuthProvider()
    _atlas_auth_kwargs = dict(
        auth_server_provider=_atlas_oauth_provider,
        auth=AuthSettings(
            issuer_url=_OAUTH_ISSUER,
            resource_server_url=_OAUTH_RESOURCE,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[],
        ),
    )

mcp = FastMCP(
    "AtlasMCP",
    instructions=_resolve_mcp_instructions(),
    host=os.environ.get("ATLAS_MCP_HOST", DEFAULT_HOST),
    port=int(os.environ.get("ATLAS_MCP_PORT", str(DEFAULT_PORT))),
    streamable_http_path=os.environ.get("ATLAS_MCP_PATH", DEFAULT_MCP_PATH),
    **_atlas_auth_kwargs,
)


@mcp.tool()
def stack_summary() -> dict[str, Any]:
    """Return counts of each entity type in the store. Use this for orientation."""
    store = _get_store()
    summary: dict[str, Any] = {}
    for kind, entities in store.items():
        summary[kind] = len(entities)
    return summary


# ------------------------------------------------------------- demand log ---
# The capability ledger measures SUPPLY: what exists, and whether it ran.
# Nothing recorded DEMAND — what was asked for, and whether the stack could
# answer. Without demand, a gap is only findable by a human reading 150 rows,
# which is the manual audit this is meant to replace. Every ask appends one
# line here; automations/scripts/capability_demand.py rolls it up.
DEFAULT_DEMAND_LOG = "/opt/stack/services/automations/state/capability_asks.jsonl"


def _demand_log() -> Path:
    """Resolved per call, not at import: tests point it at a fixture without
    reloading the module, and reloading was silently returning the cached
    module — every 'reloaded' assertion was checking the first import."""
    return Path(os.environ.get("ATLAS_DEMAND_LOG", DEFAULT_DEMAND_LOG))

ASK_OUTCOMES = {
    "hit",    # something exists and is usable (a LIVE or IDLE match)
    "stale",  # only DEAD/UNKNOWN matched — something exists, nothing usable
    "miss",   # nothing matched at all
    "built",  # the ask was answered by building the capability
    "error",  # the lookup itself failed; NOT evidence about the stack
}


def _record_ask(query: str, outcome: str, source: str,
                matched: list[str] | tuple[str, ...] = (), note: str = "") -> bool:
    """Append one ask to the demand log.

    Best-effort by design: a telemetry write must never fail the tool call it is
    observing. A demand log that can take down find_capability would be worse
    than no demand log.
    """
    record = {
        "at": _now_iso(),
        "query": " ".join(str(query or "").split())[:300],
        "outcome": outcome if outcome in ASK_OUTCOMES else "error",
        "source": source,
        "top": matched[0] if matched else None,
        "matched": list(matched)[:3],
        "note": note[:200],
    }
    try:
        log = _demand_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        # One line, one write, O_APPEND: concurrent writers interleave records,
        # never halves of a record.
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logging.getLogger(__name__).debug("demand log write failed", exc_info=True)
        return False


@mcp.tool()
def record_capability_ask(query: str, outcome: str, capability_ids: str = "",
                          note: str = "") -> dict[str, Any]:
    """Record a capability ask that was answered WITHOUT calling find_capability.

    The SessionStart digest deliberately removes the lookup round-trip, so most
    asks now get answered straight from context and leave no trace. That is a
    win for latency and a hole in the demand signal; this closes it.

    Call this once per capability question you answer — "do we have something
    for X", "what runs Y", or a request that turned out to need a build.

      query           what was asked, in the asker's words
      outcome         hit | stale | miss | built
                        hit    a usable capability already existed
                        stale  something exists but is DEAD/UNKNOWN/unusable
                        miss   nothing exists for this
                        built  the gap was closed by building it
      capability_ids  comma-separated ids you pointed at, most relevant first
      note            anything the query alone would not tell a later reader
    """
    if outcome not in ASK_OUTCOMES:
        return {"recorded": False,
                "error": f"outcome must be one of {sorted(ASK_OUTCOMES - {'error'})}"}
    ids = [i.strip() for i in (capability_ids or "").split(",") if i.strip()]
    ok = _record_ask(query, outcome, source="agent", matched=ids, note=note)
    return {"recorded": ok, "log": str(_demand_log()),
            "note": None if ok else "demand log unwritable; the ask was not recorded"}


@mcp.tool()
def find_capability(query: str, include_unknown: bool = True, limit: int = 12) -> dict[str, Any]:
    """Find a stack capability by what it does, and report whether it is still used.

    Answers "do we already have something for X?" — the question that keeps
    getting answered wrong because capabilities are built and then forgotten.
    Searches the capability ledger (services, reverse-proxied surfaces, and
    Atlas MCP tools) by id, name, summary, and invocation path.

    Each hit carries `state` and `evidence`:
      LIVE     used within 30 days; evidence names the journal entry or request count
      IDLE     an invocation path exists, but nothing walked it in that window
      DEAD     no invocation path found at all
      UNKNOWN  no evidence source exists for this class — NOT a claim that it works

    The ledger is written daily by capability-ledger.timer on the host. If it is
    missing or stale, that is reported rather than papered over.
    """
    ledger_path = Path(os.environ.get(
        "ATLAS_CAPABILITY_LEDGER",
        "/opt/stack/services/automations/state/capability_ledger.json"))
    try:
        ledger = json.loads(ledger_path.read_text())
    except Exception as exc:
        # Recorded as `error`, never as `miss`: a broken ledger is not evidence
        # that the stack lacks the capability, and scoring it as a gap would
        # manufacture build proposals out of an outage.
        _record_ask(query, "error", "find_capability", note=f"ledger unreadable: {exc}")
        return {"error": True,
                "reason": f"capability ledger unreadable ({exc}); "
                          "check capability-ledger.timer on the host",
                "results": []}

    # Question words carry no signal and match everything — "queue a build"
    # otherwise scores all 150 rows on the "a".
    STOPWORDS = {
        "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "is", "are",
        "do", "does", "we", "i", "my", "our", "what", "which", "how", "can", "any",
        "have", "has", "that", "this", "with", "use", "used", "using", "run",
        "something", "anything", "thing", "stuff", "get", "there",
    }
    # Crude suffix stripping, enough to bridge execution/executor/executes and
    # alerting/alert. Not a real stemmer, and not pretending to be one.
    SUFFIXES = ("tion", "ing", "ion", "ers", "er", "or", "es", "ed", "ly", "s")

    def stem(word: str) -> str:
        for suf in SUFFIXES:
            if len(word) - len(suf) >= 4 and word.endswith(suf):
                return word[: -len(suf)]
        return word

    terms = [t for t in (w.strip(".,?!'\"") for w in query.lower().split())
             if t and t not in STOPWORDS and len(t) > 2]
    if not terms:
        _record_ask(query, "error", "find_capability", note="no searchable terms")
        return {"query": query, "match_count": 0, "returned": 0, "results": [],
                "note": "query had no searchable terms after stopword removal"}

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in ledger.get("rows", []):
        if not include_unknown and row["state"] == "UNKNOWN":
            continue
        ident = f"{row.get('id', '')} {row.get('name', '')}".lower()
        haystack = " ".join((
            ident, row.get("search_text", ""), row.get("summary", ""),
            row.get("invoke", ""),
        )).lower()

        # Weight identifier hits above prose hits so `talos` beats a service
        # that merely mentions talos in its summary. Exact beats stemmed.
        score = 0
        for t in terms:
            s = stem(t)
            if t in ident:
                score += 10
            elif s in ident:
                score += 6
            elif t in haystack:
                score += 2
            elif s in haystack:
                score += 1
        if score:
            scored.append((score, row))

    order = {"LIVE": 0, "IDLE": 1, "DEAD": 2, "UNKNOWN": 3}
    scored.sort(key=lambda p: (-p[0], order.get(p[1]["state"], 9), p[1]["id"]))

    results = [{
        "id": r["id"],
        "class": r["class"],
        "state": r["state"],
        "summary": r.get("summary", ""),
        "invoke": r.get("invoke", ""),
        "last_used": r.get("last_used_human"),
        "evidence": r.get("evidence", ""),
    } for _, r in scored[:limit]]

    # `stale` is the interesting outcome: the query DID match something, but
    # nothing usable. Folding it into `hit` would hide the case where a
    # capability exists on paper and cannot answer the ask.
    if not results:
        outcome = "miss"
    elif any(r["state"] in ("LIVE", "IDLE") for r in results):
        outcome = "hit"
    else:
        outcome = "stale"
    _record_ask(query, outcome, "find_capability", matched=[r["id"] for r in results[:3]])

    return {
        "query": query,
        "generated_at": ledger.get("generated_at"),
        "match_count": len(scored),
        "returned": len(results),
        "results": results,
        "note": "UNKNOWN means unmeasured, not working. Read `evidence` before acting.",
    }


@mcp.tool()
def list_projects(category: str = "", status: str = "") -> list[dict[str, Any]]:
    """List all projects. Optionally filter by category (e.g. 'current', 'live', 'blocked', 'defer', 'archive') or status."""
    store = _get_store()
    projects = store.get("project", {})
    result = []
    for pid, project in sorted(projects.items()):
        p = _model_to_dict(project)
        if category and p.get("category", {}).get("value_id") != category:
            continue
        if status and p.get("status", {}).get("value_id") != status:
            continue
        result.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "category": p.get("category", {}).get("value_id"),
            "status": p.get("status", {}).get("value_id"),
            "summary": p.get("summary", "").strip(),
        })
    return result


@mcp.tool()
def get_project(id: str) -> dict[str, Any]:
    """Return the full project entity for the given id."""
    store = _get_store()
    projects = store.get("project", {})
    project = projects.get(id)
    if project is None:
        ids = sorted(projects.keys())
        raise ValueError(f"Project '{id}' not found. Known ids: {ids}")
    return _model_to_dict(project)


@mcp.tool()
def list_tasks(project: str = "", status: str = "", priority: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List task entities. Optional filters: project (id or exact name), status, priority."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if status and status not in _TASK_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_TASK_STATUS_VALUES)}")
    if priority and priority not in _TASK_PRIORITY_VALUES:
        raise ValueError(f"priority must be one of: {sorted(_TASK_PRIORITY_VALUES)}")

    project_id = _resolve_project_id(project) if project else ""

    store = _get_store()
    tasks = store.get("task", {})
    result: list[dict[str, Any]] = []
    for task_id, task in sorted(tasks.items()):
        payload = _model_to_dict(task)
        if project_id and payload.get("project_id") != project_id:
            continue
        if status and payload.get("status") != status:
            continue
        if priority and payload.get("priority") != priority:
            continue

        result.append(
            {
                "id": task_id,
                "project_id": payload.get("project_id"),
                "title": payload.get("title"),
                "status": payload.get("status"),
                "priority": payload.get("priority"),
                "next_action": payload.get("next_action"),
                "updated_at": payload.get("updated_at"),
            }
        )
        if len(result) >= limit:
            break
    return result


@mcp.tool()
def list_open_tasks(project: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List open task entities (status in open, in_progress, blocked)."""
    open_states = {"open", "in_progress", "blocked"}
    tasks = list_tasks(project=project, status="", priority="", limit=1000)
    filtered = [task for task in tasks if task.get("status") in open_states]
    return filtered[:limit]


@mcp.tool()
def list_actionable_tasks(project: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List actionable task entities (status in open, in_progress)."""
    actionable_states = {"open", "in_progress"}
    tasks = list_tasks(project=project, status="", priority="", limit=1000)
    filtered = [task for task in tasks if task.get("status") in actionable_states]
    return filtered[:limit]


@mcp.tool()
def get_task(id: str) -> dict[str, Any]:
    """Return the full task entity for the given id."""
    store = _get_store()
    tasks = store.get("task", {})
    task = tasks.get(id)
    if task is None:
        ids = sorted(tasks.keys())
        raise ValueError(f"Task '{id}' not found. Known ids: {ids}")
    return _model_to_dict(task)


@mcp.tool()
def list_sessions(
    source: str = "",
    status: str = "",
    lifecycle: str = "",
    user_id: int = 0,
    project_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List session entities with optional filters."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if status and status not in _SESSION_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_SESSION_STATUS_VALUES)}")
    if lifecycle and lifecycle not in _SESSION_LIFECYCLE_VALUES:
        raise ValueError(f"lifecycle must be one of: {sorted(_SESSION_LIFECYCLE_VALUES)}")

    store = _get_store()
    sessions = store.get("session", {})
    result: list[dict[str, Any]] = []
    for session_id, session in sorted(sessions.items()):
        payload = _model_to_dict(session)
        if source and payload.get("source") != source:
            continue
        if status and payload.get("status") != status:
            continue
        if lifecycle and payload.get("lifecycle", "active") != lifecycle:
            continue
        if user_id and int(payload.get("user_id", 0)) != user_id:
            continue
        if project_id:
            project_ids = payload.get("project_ids") or []
            if project_id not in project_ids:
                continue

        result.append(
            {
                "id": session_id,
                "source": payload.get("source"),
                "user_id": payload.get("user_id"),
                "status": payload.get("status"),
                "lifecycle": payload.get("lifecycle", "active"),
                "timestamp": payload.get("timestamp"),
                "summary": payload.get("summary"),
                "project_ids": payload.get("project_ids", []),
                "updated_at": payload.get("updated_at"),
            }
        )
        if len(result) >= limit:
            break
    return result


@mcp.tool()
def get_session(id: str) -> dict[str, Any]:
    """Return the full session entity for the given id."""
    store = _get_store()
    sessions = store.get("session", {})
    session = sessions.get(id)
    if session is None:
        ids = sorted(sessions.keys())
        raise ValueError(f"Session '{id}' not found. Known ids: {ids}")
    return _model_to_dict(session)


@mcp.tool()
def list_services() -> list[dict[str, Any]]:
    """List all services with key operational fields."""
    store = _get_store()
    services = store.get("service", {})
    result = []
    for sid, service in sorted(services.items()):
        s = _model_to_dict(service)
        result.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "service_type": s.get("service_type", {}).get("value_id"),
            "lifecycle": s.get("lifecycle", {}).get("value_id"),
            "host": s.get("host", {}).get("id"),
            "port": s.get("port"),
            "health_endpoint": s.get("health_endpoint"),
            "publication_disposition": s.get("publication_disposition"),
        })
    return result


@mcp.tool()
def list_consumer_profiles() -> list[dict[str, Any]]:
    """List consumer-profile entities with key routing and policy fields."""
    store = _get_store()
    profiles = store.get("consumer_profile", {})
    result = []
    for profile_id, profile in sorted(profiles.items()):
        p = _model_to_dict(profile)
        result.append(
            {
                "id": p.get("id"),
                "display_name": p.get("display_name"),
                "input_modality": p.get("input_modality", {}).get("value_id"),
                "auth_principal": p.get("auth_principal"),
                "allowed_action_tiers": [
                    item.get("value_id") for item in (p.get("allowed_action_tiers") or []) if isinstance(item, dict)
                ],
                "confirm_channel": p.get("confirm_channel", {}).get("value_id"),
                "response_shape": p.get("response_shape", {}).get("value_id"),
                "session_entity_profile": p.get("session_entity_profile", {}).get("value_id"),
            }
        )
    return result


@mcp.tool()
def get_consumer_profile(id: str) -> dict[str, Any]:
    """Return the full consumer-profile entity for the given id."""
    store = _get_store()
    profiles = store.get("consumer_profile", {})
    profile = profiles.get(id)
    if profile is None:
        ids = sorted(profiles.keys())
        raise ValueError(f"Consumer profile '{id}' not found. Known ids: {ids}")
    return _model_to_dict(profile)


@mcp.tool()
def get_service(id: str) -> dict[str, Any]:
    """Return the full service entity for the given id."""
    store = _get_store()
    services = store.get("service", {})
    service = services.get(id)
    if service is None:
        ids = sorted(services.keys())
        raise ValueError(f"Service '{id}' not found. Known ids: {ids}")
    return _model_to_dict(service)


@mcp.tool()
def list_runtimes() -> list[dict[str, Any]]:
    """List runtime entities — the canonical capability registry. Each runtime is a
    deterministic capability callable from any surface; a tool's tier (vocab:action_tiers)
    pairs with a consumer's allowed_action_tiers for entitlement. Surfaces use this to
    discover what they can invoke and where (executor) and what to subscribe to (event_subject)."""
    store = _get_store()
    runtimes = store.get("runtime", {})
    result = []
    for rid, rt in sorted(runtimes.items()):
        r = _model_to_dict(rt)
        result.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "lifecycle": r.get("lifecycle", {}).get("value_id"),
            "executor": r.get("executor", {}).get("id"),
            "event_subject": r.get("event_subject"),
            "tools": [
                {
                    "name": t.get("name"),
                    "mutates": t.get("mutates"),
                    "tier": t.get("tier", {}).get("value_id"),
                }
                for t in (r.get("tools") or [])
            ],
        })
    return result


@mcp.tool()
def get_runtime(id: str) -> dict[str, Any]:
    """Return the full runtime entity (tools + args_schema, executor, event_subject, spec_doc)."""
    store = _get_store()
    runtimes = store.get("runtime", {})
    rt = runtimes.get(id)
    if rt is None:
        ids = sorted(runtimes.keys())
        raise ValueError(f"Runtime '{id}' not found. Known ids: {ids}")
    return _model_to_dict(rt)


@mcp.tool()
def get_server(id: str) -> dict[str, Any]:
    """Return the full server entity for the given id."""
    store = _get_store()
    servers = store.get("server", {})
    server = servers.get(id)
    if server is None:
        ids = sorted(servers.keys())
        raise ValueError(f"Server '{id}' not found. Known ids: {ids}")
    return _model_to_dict(server)


@mcp.tool()
def list_servers() -> list[dict[str, Any]]:
    """List server entities — the physical/virtual hosts services are deployed on."""
    store = _get_store()
    servers = store.get("server", {})
    result = []
    for sid, srv in sorted(servers.items()):
        s = _model_to_dict(srv)
        result.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "hostname": s.get("hostname"),
            "ip": s.get("ip"),
            "hardware": s.get("hardware"),
            "node_class": s.get("node_class"),
        })
    return result


@mcp.tool()
def list_vocabularies() -> list[dict[str, Any]]:
    """List every vocabulary with its legal value_ids inline.

    Call this before any write. Vocab-valued arguments on add_*/update_* tools take a
    bare value_id (e.g. 'systemd_unit', not 'vocab:service_types:systemd_unit'), and an
    unknown value_id is rejected before the write — nothing is committed. This is the
    only way to discover the legal set; guessing is what put four unresolvable refs into
    a live consumer_profile in 2026-07.
    """
    store = _get_store()
    vocabs = store.get("vocabulary", {})
    result = []
    for vid, vocab in sorted(vocabs.items()):
        v = _model_to_dict(vocab)
        values = v.get("values") or []
        result.append({
            "id": v.get("id"),
            "name": v.get("name"),
            "purpose": v.get("purpose"),
            "extension_policy": v.get("extension_policy"),
            "value_ids": [item.get("id") for item in values if not item.get("deprecated")],
            "deprecated_value_ids": [item.get("id") for item in values if item.get("deprecated")],
            "value_count": len(values),
        })
    return result


@mcp.tool()
def get_vocabulary(id: str) -> dict[str, Any]:
    """Return one vocabulary with all its values (id, name, description, semantics, deprecated).

    For just the legal value_ids across every vocabulary at once, use list_vocabularies().
    """
    store = _get_store()
    vocabs = store.get("vocabulary", {})
    vocab = vocabs.get(id)
    if vocab is None:
        ids = sorted(vocabs.keys())
        raise ValueError(f"Vocabulary '{id}' not found. Known ids: {ids}")
    return _model_to_dict(vocab)


@mcp.tool()
def list_rules(scope: str = "", severity: str = "") -> list[dict[str, Any]]:
    """List rules. Optionally filter by scope or severity."""
    store = _get_store()
    rules = store.get("rule", {})
    result = []
    for rid, rule in sorted(rules.items()):
        r = _model_to_dict(rule)
        if scope and r.get("scope", {}).get("value_id") != scope:
            continue
        if severity and r.get("severity", {}).get("value_id") != severity:
            continue
        result.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "scope": r.get("scope", {}).get("value_id"),
            "severity": r.get("severity", {}).get("value_id"),
            "applies_to": r.get("applies_to"),
            "check_kind": r.get("check_kind", {}).get("value_id"),
            "fix_tier": r.get("fix_tier", {}).get("value_id"),
            "enforcement_point": r.get("enforcement_point", {}).get("value_id"),
        })
    return result


@mcp.tool()
def get_rule(id: str) -> dict[str, Any]:
    """Return the full rule entity for the given id."""
    store = _get_store()
    rules = store.get("rule", {})
    rule = rules.get(id)
    if rule is None:
        ids = sorted(rules.keys())
        raise ValueError(f"Rule '{id}' not found. Known ids: {ids}")
    return _model_to_dict(rule)


# Sentinel for update_rule: "" already means "argument not supplied" across every write
# tool, so it cannot express "set this field to null" — which is exactly what moving a
# rule to fix_tier=flag requires.
_NULL_SENTINEL = "__NULL__"


def _enforce_fix_tier_contract(fix_tier: str, fix_action: str | None) -> None:
    """Cross-field contract from schemas/rule.py, enforced pre-write.

    Redundant with the schema (which the write gate now runs before committing), but it
    returns a precise, actionable message instead of a pydantic traceback.
    """
    has_action = bool(fix_action and fix_action.strip())
    if fix_tier == "flag" and has_action:
        raise ValueError(
            "fix_action must be empty when fix_tier is 'flag' (a flag only reports; it "
            f"prescribes no fix). Pass fix_action='{_NULL_SENTINEL}' to clear it."
        )
    if fix_tier in {"auto", "propose", "block"} and not has_action:
        raise ValueError(
            f"fix_action is required when fix_tier is '{fix_tier}' — it must state the "
            "exact remediation steps."
        )


@mcp.tool()
def add_rule(
    id: str,
    name: str,
    description: str,
    scope: str,
    severity: str,
    applies_to: str,
    check_kind: str,
    check_definition: str,
    fix_tier: str,
    enforcement_point: str,
    on_violation: str,
    fix_action: str = "",
    min_plan_number: int = 0,
    authored_by: str = "atlas-mcp",
    confirm: bool = False,
) -> dict[str, Any]:
    """Add a new rule entity — a structural assertion Atlas enforces across the stack.

    CONFIRM-GATED, unlike the other add_* tools. Rules govern agent behaviour, so an agent
    that could silently author its own governing rules could author the one that authorizes
    what it wants to do. Returns a preview when confirm=False (default); call again with
    confirm=True and the same args to apply.

    Vocab args take a bare value_id and are validated before the write —
    call list_vocabularies() for the legal sets:
      scope: rule_scopes            severity: rule_severities
      check_kind: rule_check_kinds  fix_tier: rule_fix_tiers
      enforcement_point: enforcement_points

    fix_action is REQUIRED when fix_tier is auto, propose, or block (it must give the exact
    remediation steps), and MUST be empty when fix_tier is 'flag' (a flag only reports).

    applies_to: what the rule binds to, e.g. 'service_entity:mcp_http', 'plan_document'.
    check_definition: the assertion itself, interpreted per check_kind.
    on_violation: what an agent should do when the rule trips.
    """
    store = _get_store()
    if id in store.get("rule", {}):
        raise ValueError(f"Rule '{id}' already exists. Use update_rule to modify it.")

    _enforce_fix_tier_contract(fix_tier, fix_action)

    now = _now_iso()
    data: dict[str, Any] = {
        "id": id,
        "name": name,
        "description": description,
        "scope": f"vocab:rule_scopes:{scope}",
        "severity": f"vocab:rule_severities:{severity}",
        "applies_to": applies_to,
        "check_kind": f"vocab:rule_check_kinds:{check_kind}",
        "check_definition": check_definition,
        "fix_tier": f"vocab:rule_fix_tiers:{fix_tier}",
        "fix_action": fix_action.strip() if fix_action.strip() else None,
        "enforcement_point": f"vocab:enforcement_points:{enforcement_point}",
        "on_violation": on_violation,
        "authored_by": authored_by,
        "created_at": now,
        "updated_at": now,
    }
    if min_plan_number:
        data["min_plan_number"] = min_plan_number

    if not confirm:
        check = _validate_entity("rules", id, data)
        return {
            "action": "preview",
            "id": id,
            "after": data,
            "validation": {"errors": check.errors, "warnings": check.warnings},
            "note": "Call add_rule(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("rules", id, data, f"feat: add rule {id} via Atlas Write API")


@mcp.tool()
def update_rule(
    id: str,
    confirm: bool = False,
    name: str = "",
    description: str = "",
    scope: str = "",
    severity: str = "",
    applies_to: str = "",
    check_kind: str = "",
    check_definition: str = "",
    fix_tier: str = "",
    fix_action: str = "",
    enforcement_point: str = "",
    on_violation: str = "",
    min_plan_number: int = 0,
) -> dict[str, Any]:
    """Update fields on an existing rule entity.

    Returns a before/after preview when confirm=False (default). Call again with confirm=True
    and the same args to apply. Empty/0 arguments mean "leave unchanged".

    To CLEAR fix_action (required when moving a rule to fix_tier='flag'), pass the sentinel
    fix_action='__NULL__' — "" means "not supplied", so it cannot express "set to null".

    The fix_tier/fix_action contract is checked against the MERGED result, not just the
    arguments: update_rule(fix_tier='flag') alone is rejected if the rule already has a
    fix_action, because the merged entity would be invalid.
    """
    store = _get_store()
    rules = store.get("rule", {})
    if id not in rules:
        raise ValueError(f"Rule '{id}' not found. Known ids: {sorted(rules.keys())}")

    rel_path = f"entities/rules/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    after = dict(current_data)
    if name:
        after["name"] = name
    if description:
        after["description"] = description
    if scope:
        after["scope"] = f"vocab:rule_scopes:{scope}"
    if severity:
        after["severity"] = f"vocab:rule_severities:{severity}"
    if applies_to:
        after["applies_to"] = applies_to
    if check_kind:
        after["check_kind"] = f"vocab:rule_check_kinds:{check_kind}"
    if check_definition:
        after["check_definition"] = check_definition
    if fix_tier:
        after["fix_tier"] = f"vocab:rule_fix_tiers:{fix_tier}"
    if fix_action == _NULL_SENTINEL:
        after["fix_action"] = None
    elif fix_action:
        after["fix_action"] = fix_action
    if enforcement_point:
        after["enforcement_point"] = f"vocab:enforcement_points:{enforcement_point}"
    if on_violation:
        after["on_violation"] = on_violation
    if min_plan_number:
        after["min_plan_number"] = min_plan_number

    # Evaluate the contract against the merged entity, not the arguments — otherwise
    # update_rule(fix_tier="flag") would slip past while the rule keeps its old fix_action.
    merged_tier = str(after.get("fix_tier", "")).rsplit(":", 1)[-1]
    _enforce_fix_tier_contract(merged_tier, after.get("fix_action"))

    after["updated_at"] = _now_iso()

    if not confirm:
        check = _validate_entity("rules", id, after)
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "validation": {"errors": check.errors, "warnings": check.warnings},
            "note": "Call update_rule(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("rules", id, after, f"chore: update rule {id} via Atlas Write API")


@mcp.tool()
def list_publications() -> list[dict[str, Any]]:
    """List publication entities — the published public surface (GitHub repos/docs) and
    their publication contract. Each is drift-checked by the publication-drift probe."""
    store = _get_store()
    pubs = store.get("publication", {})
    result = []
    for pid, pub in sorted(pubs.items()):
        p = _model_to_dict(pub)
        result.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "github_repo": p.get("github_repo"),
            "public": p.get("public"),
            "source_kind": p.get("source_kind"),
            "leak_gate_profile": p.get("leak_gate_profile"),
            "last_published_commit": p.get("last_published_commit"),
            "depends_on": [f"{d.get('entity_type')}:{d.get('id')}" for d in (p.get("depends_on") or [])],
        })
    return result


@mcp.tool()
def get_publication(id: str) -> dict[str, Any]:
    """Return the full publication entity (contract, source, deps, freshness policy)."""
    store = _get_store()
    pubs = store.get("publication", {})
    pub = pubs.get(id)
    if pub is None:
        raise ValueError(f"Publication '{id}' not found. Known ids: {sorted(pubs.keys())}")
    return _model_to_dict(pub)


@mcp.tool()
def add_publication(
    id: str,
    name: str,
    github_repo: str,
    remote: str = "",
    public: bool = True,
    local_source_path: str = "",
    source_kind: str = "direct",
    leak_gate_profile: str = "base",
    last_published_commit: str = "",
    depends_on: list[str] | None = None,
    readme_freshness_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a new publication entity (a published repo + its publication contract).
    Autonomous — writes and commits immediately. source_kind is 'direct' or 'derived';
    leak_gate_profile is one of secrets|base|career; depends_on entries are 'publication:<id>'."""
    store = _get_store()
    if id in store.get("publication", {}):
        raise ValueError(f"Publication '{id}' already exists. Use update_publication to modify it.")
    now = _now_iso()
    data: dict[str, Any] = {
        "id": id,
        "name": name,
        "github_repo": github_repo,
        "public": public,
        "source_kind": source_kind,
        "leak_gate_profile": leak_gate_profile,
        "depends_on": depends_on or [],
        "created_at": now,
        "updated_at": now,
    }
    if remote:
        data["remote"] = remote
    if local_source_path:
        data["local_source_path"] = local_source_path
    if last_published_commit:
        data["last_published_commit"] = last_published_commit
    if readme_freshness_policy is not None:
        data["readme_freshness_policy"] = readme_freshness_policy
    return _write_and_commit("publications", id, data, f"feat: add publication {id} via Atlas Write API")


@mcp.tool()
def update_publication(
    id: str,
    confirm: bool = False,
    last_published_commit: str = "",
    last_usage_audit: str = "",
    last_deep_check: str = "",
    leak_gate_profile: str = "",
    public: bool | None = None,
    source_kind: str = "",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Update fields on a publication entity. Preview unless confirm=True.
    Timestamp fields (last_usage_audit/last_deep_check) accept ISO-8601 or the literal 'now'."""
    store = _get_store()
    pubs = store.get("publication", {})
    if id not in pubs:
        raise ValueError(f"Publication '{id}' not found. Known ids: {sorted(pubs.keys())}")
    rel_path = f"entities/publications/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    def _ts(v: str) -> str:
        return _now_iso() if v == "now" else v

    if last_published_commit:
        after["last_published_commit"] = last_published_commit
    if last_usage_audit:
        after["last_usage_audit"] = _ts(last_usage_audit)
    if last_deep_check:
        after["last_deep_check"] = _ts(last_deep_check)
    if leak_gate_profile:
        after["leak_gate_profile"] = leak_gate_profile
    if public is not None:
        after["public"] = public
    if source_kind:
        after["source_kind"] = source_kind
    if depends_on is not None:
        after["depends_on"] = depends_on
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_publication(id=..., confirm=True, ...) with the same args to apply.",
        }
    return _write_and_commit("publications", id, after, f"chore: update publication {id} via Atlas Write API")


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

@mcp.tool()
def add_project(
    id: str,
    name: str,
    summary: str,
    category: str,
    status: str,
    concept_doc: str,
    gdrive_folder: str = "",
    code_repo: str = "",
    remote: str = "",
    domain_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Add a new project entity to the Atlas store. Autonomous — writes and commits immediately.

    Vocab value_ids are validated before the write; an unknown value is rejected and
    nothing is committed. Call list_vocabularies() for the live legal set.

    category: vocab value_id — one of: current, live, blocked, defer, archive
    status: vocab value_id — one of: concept, scaffolded, in_progress, active,
            installed, parked, archived
    concept_doc: path relative to services root, e.g. 'docs/kb/Projects/Foo/10-CONCEPT/FOO_CONCEPT.md'
    gdrive_folder: legacy Drive path metadata, optional; Drive retired as authoring surface 2026-05-09
    domain_tags: optional list of lowercase domain labels
    """
    store = _get_store()
    if id in store.get("project", {}):
        raise ValueError(f"Project '{id}' already exists. Use update_project to modify it.")

    validated_tags = _validate_domain_tags(domain_tags)
    now = _now_iso()
    data: dict[str, Any] = {
        "id": id,
        "name": name,
        "summary": summary,
        "category": f"vocab:lifecycle_categories:{category}",
        "status": f"vocab:project_statuses:{status}",
        "concept_doc": concept_doc,
        "created_at": now,
        "updated_at": now,
    }
    if gdrive_folder:
        data["gdrive_folder"] = gdrive_folder
    if code_repo:
        data["code_repo"] = code_repo
    if remote:
        data["remote"] = remote
    if validated_tags is not None:
        data["domain_tags"] = validated_tags

    return _write_and_commit("projects", id, data, f"feat: add project {id} via Atlas Write API")


@mcp.tool()
def update_project(
    id: str,
    confirm: bool = False,
    status: str = "",
    status_detail: str = "",
    category: str = "",
    summary: str = "",
    last_done: str = "",
    next_action: str = "",
    blocked_on: str = "",
    code_repo: str = "",
    remote: str = "",
    domain_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Update fields on an existing project entity.

    Returns a before/after preview when confirm=False (default). Call again with confirm=True to apply.
    Only non-empty scalar arguments are applied. status/category accept value_ids (e.g. 'active', 'current').
    domain_tags is applied when provided (including an empty list to clear tags).
    """
    store = _get_store()
    projects = store.get("project", {})
    if id not in projects:
        raise ValueError(f"Project '{id}' not found. Known ids: {sorted(projects.keys())}")

    rel_path = f"entities/projects/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    validated_tags = _validate_domain_tags(domain_tags)
    after = dict(current_data)
    if status:
        after["status"] = f"vocab:project_statuses:{status}"
    if status_detail:
        after["status_detail"] = status_detail
    if category:
        after["category"] = f"vocab:lifecycle_categories:{category}"
    if summary:
        after["summary"] = summary
    if last_done:
        after["last_done"] = last_done
    if next_action:
        after["next_action"] = next_action
    if blocked_on:
        after["blocked_on"] = blocked_on
    if code_repo:
        after["code_repo"] = code_repo
    if remote:
        after["remote"] = remote
    if validated_tags is not None:
        after["domain_tags"] = validated_tags
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_project(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("projects", id, after, f"chore: update project {id} via Atlas Write API")


@mcp.tool()
def add_consumer_profile(
    id: str,
    display_name: str,
    input_modality: str,
    auth_principal: str,
    allowed_action_tiers: list[str],
    autopilot_eligibility: dict[str, bool],
    confirm_channel: str,
    response_shape: str,
    session_entity_profile: str,
    tool_scope: list[str],
    explainability_payload: dict[str, Any] | None = None,
    override_policy_boundaries: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Add a new consumer-profile entity to the Atlas store. Autonomous — writes and commits immediately."""
    store = _get_store()
    if id in store.get("consumer_profile", {}):
        raise ValueError(f"Consumer profile '{id}' already exists. Use update_consumer_profile to modify it.")

    if not allowed_action_tiers:
        raise ValueError("allowed_action_tiers must include at least one tier")
    if not tool_scope:
        raise ValueError("tool_scope must include at least one entry")

    now = _now_iso()
    data: dict[str, Any] = {
        "id": id,
        "display_name": display_name,
        "input_modality": f"vocab:consumer_input_modalities:{input_modality}",
        "auth_principal": auth_principal,
        "allowed_action_tiers": [f"vocab:action_tiers:{item}" for item in allowed_action_tiers],
        "autopilot_eligibility": autopilot_eligibility,
        "confirm_channel": f"vocab:consumer_confirm_channels:{confirm_channel}",
        "response_shape": f"vocab:consumer_response_shapes:{response_shape}",
        "session_entity_profile": f"vocab:session_entity_profiles:{session_entity_profile}",
        "tool_scope": tool_scope,
        "created_at": now,
        "updated_at": now,
    }
    if explainability_payload is not None:
        data["explainability_payload"] = explainability_payload
    if override_policy_boundaries is not None:
        data["override_policy_boundaries"] = override_policy_boundaries
    if notes:
        data["notes"] = notes

    return _write_and_commit(
        "consumer_profiles",
        id,
        data,
        f"feat: add consumer profile {id} via Atlas Write API",
    )


@mcp.tool()
def update_consumer_profile(
    id: str,
    confirm: bool = False,
    display_name: str = "",
    input_modality: str = "",
    auth_principal: str = "",
    allowed_action_tiers: list[str] | None = None,
    autopilot_eligibility: dict[str, bool] | None = None,
    confirm_channel: str = "",
    response_shape: str = "",
    session_entity_profile: str = "",
    tool_scope: list[str] | None = None,
    explainability_payload: dict[str, Any] | None = None,
    override_policy_boundaries: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Update fields on an existing consumer-profile entity via propose-confirm pattern."""
    store = _get_store()
    profiles = store.get("consumer_profile", {})
    if id not in profiles:
        raise ValueError(f"Consumer profile '{id}' not found. Known ids: {sorted(profiles.keys())}")

    rel_path = f"entities/consumer_profiles/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if display_name:
        after["display_name"] = display_name
    if input_modality:
        after["input_modality"] = f"vocab:consumer_input_modalities:{input_modality}"
    if auth_principal:
        after["auth_principal"] = auth_principal
    if allowed_action_tiers is not None:
        if not allowed_action_tiers:
            raise ValueError("allowed_action_tiers cannot be empty")
        after["allowed_action_tiers"] = [f"vocab:action_tiers:{item}" for item in allowed_action_tiers]
    if autopilot_eligibility is not None:
        after["autopilot_eligibility"] = autopilot_eligibility
    if confirm_channel:
        after["confirm_channel"] = f"vocab:consumer_confirm_channels:{confirm_channel}"
    if response_shape:
        after["response_shape"] = f"vocab:consumer_response_shapes:{response_shape}"
    if session_entity_profile:
        after["session_entity_profile"] = f"vocab:session_entity_profiles:{session_entity_profile}"
    if tool_scope is not None:
        if not tool_scope:
            raise ValueError("tool_scope cannot be empty")
        after["tool_scope"] = tool_scope
    if explainability_payload is not None:
        after["explainability_payload"] = explainability_payload
    if override_policy_boundaries is not None:
        after["override_policy_boundaries"] = override_policy_boundaries
    if notes:
        after["notes"] = notes

    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_consumer_profile(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit(
        "consumer_profiles",
        id,
        after,
        f"chore: update consumer profile {id} via Atlas Write API",
    )


@mcp.tool()
def add_service(
    id: str,
    name: str,
    summary: str,
    service_type: str,
    lifecycle: str,
    deployment_path: str,
    port: int = 0,
    host: str = "server:example-host",
    owned_by: str = "",
    health_endpoint: str = "",
    systemd_unit: str = "",
    container_name: str = "",
    owns_units: list[str] | None = None,
    restartable: str = "",
    tier: str = "",
    expected_state: str = "",
    protected_high_use: str = "",
    remote: str = "",
    review_by: str = "",
) -> dict[str, Any]:
    """Add a new service entity to the Atlas store. Autonomous — writes and commits immediately.

    Vocab value_ids are validated before the write; an unknown value is rejected and
    nothing is committed. Call list_vocabularies() for the live legal set.

    service_type: vocab value_id — one of: mcp_http, mcp_stdio, fastapi, docker_container,
                  systemd_unit, nginx_vhost, cloudflare_worker, cloudflare_pages
    lifecycle: vocab value_id — one of: running, maintained, degraded, retired
    host: TypedRef string, default 'server:example-host'
    owned_by: TypedRef string, e.g. 'project:atlas' (optional)
    owns_units: every systemd unit and container this service owns, e.g.
                ['aegis.timer', 'aegis-digest.timer', 'aegis-metrics.timer'].
                This is the authoritative reality binding — the capability
                ledger reports anything on the box that no entity claims here.
                Declare all of them; one logical service commonly owns several
                units, and guessing the rest from name prefixes is what let
                units be absorbed by already-retired parents.
    review_by: ISO date for anything temporary. Register things you intend to
               retire immediately — the tombstone is the point — but give a
               temporary thing a date so it cannot quietly become permanent.
    """
    store = _get_store()
    if id in store.get("service", {}):
        raise ValueError(f"Service '{id}' already exists. Use update_service to modify it.")

    now = _now_iso()
    data: dict[str, Any] = {
        "id": id,
        "name": name,
        "summary": summary,
        "service_type": f"vocab:service_types:{service_type}",
        "lifecycle": f"vocab:service_lifecycles:{lifecycle}",
        "host": host,
        "deployment_path": deployment_path,
        "owned_by": owned_by or f"project:{id}",
        "created_at": now,
        "updated_at": now,
    }
    if port:
        data["port"] = port
    if health_endpoint:
        data["health_endpoint"] = health_endpoint
    if systemd_unit:
        data["systemd_unit"] = systemd_unit
    if container_name:
        data["container_name"] = container_name
    if owns_units:
        data["owns_units"] = [str(u).strip() for u in owns_units if str(u).strip()]
    if review_by:
        data["review_by"] = review_by
    if restartable:
        data["restartable"] = restartable.strip().lower() in {"1", "true", "yes", "on"}
    if tier:
        data["tier"] = tier
    if expected_state:
        data["expected_state"] = expected_state
    if protected_high_use:
        data["protected_high_use"] = protected_high_use.strip().lower() in {"1", "true", "yes", "on"}
    if remote:
        data["remote"] = remote

    return _write_and_commit("services", id, data, f"feat: add service {id} via Atlas Write API")


@mcp.tool()
def update_service(
    id: str,
    confirm: bool = False,
    lifecycle: str = "",
    port: int = 0,
    health_endpoint: str = "",
    systemd_unit: str = "",
    container_name: str = "",
    owns_units: list[str] | None = None,
    restartable: str = "",
    tier: str = "",
    expected_state: str = "",
    protected_high_use: str = "",
    source_of_truth_doc: str = "",
    summary: str = "",
    remote: str = "",
    publication_disposition: str = "",
    publication_target: str = "",
    review_by: str = "",
) -> dict[str, Any]:
    """Update fields on an existing service entity.

    Returns a before/after preview when confirm=False (default). Call again with confirm=True to apply.
    lifecycle accepts value_ids — one of: running, maintained, degraded, retired.
    publication_disposition is one of private|fold|standalone|cookbook|undecided;
    publication_target is the repo name (required for fold/standalone).
    owns_units REPLACES the existing list rather than merging — pass the full
    set of units/containers this service owns, or the ones you leave out become
    UNREGISTERED on the capability ledger.
    review_by is an ISO date for anything temporary.
    """
    store = _get_store()
    services = store.get("service", {})
    if id not in services:
        raise ValueError(f"Service '{id}' not found. Known ids: {sorted(services.keys())}")

    rel_path = f"entities/services/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    after = dict(current_data)
    if lifecycle:
        after["lifecycle"] = f"vocab:service_lifecycles:{lifecycle}"
    if port:
        after["port"] = port
    if health_endpoint:
        after["health_endpoint"] = health_endpoint
    if systemd_unit:
        after["systemd_unit"] = systemd_unit
    if container_name:
        after["container_name"] = container_name
    if owns_units is not None:
        after["owns_units"] = [str(u).strip() for u in owns_units if str(u).strip()]
    if review_by:
        after["review_by"] = review_by
    if restartable:
        after["restartable"] = restartable.strip().lower() in {"1", "true", "yes", "on"}
    if tier:
        after["tier"] = tier
    if expected_state:
        after["expected_state"] = expected_state
    if protected_high_use:
        after["protected_high_use"] = protected_high_use.strip().lower() in {"1", "true", "yes", "on"}
    if source_of_truth_doc:
        after["source_of_truth_doc"] = source_of_truth_doc
    if summary:
        after["summary"] = summary
    if remote:
        after["remote"] = remote
    if publication_disposition:
        after["publication_disposition"] = publication_disposition
    if publication_target:
        after["publication_target"] = publication_target
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_service(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("services", id, after, f"chore: update service {id} via Atlas Write API")


@mcp.tool()
def retire_service(id: str, confirm: bool = False, reason: str = "",
                   superseded_by: str = "") -> dict[str, Any]:
    """Set a service lifecycle to 'retired' in the Atlas store.

    Returns a preview when confirm=False (default). Call again with confirm=True to apply.

    `reason` and `superseded_by` are what make a retirement useful later. A bare
    lifecycle flip records that something is gone but not why, so the same thing
    gets rebuilt six months on. Say what it did and what replaced it.
    """
    store = _get_store()
    services = store.get("service", {})
    if id not in services:
        raise ValueError(f"Service '{id}' not found. Known ids: {sorted(services.keys())}")

    rel_path = f"entities/services/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    after = dict(current_data)
    after["lifecycle"] = "vocab:service_lifecycles:retired"
    after["retired_at"] = _now_iso()
    if reason:
        after["retired_reason"] = reason
    if superseded_by:
        after["superseded_by"] = superseded_by
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "current_lifecycle": current_data.get("lifecycle"),
            "new_lifecycle": "vocab:service_lifecycles:retired",
            "retired_reason": reason or "(none given — say why, or this is a dead end later)",
            "superseded_by": superseded_by or "(nothing named)",
            "note": "Call retire_service(id=..., confirm=True) to apply.",
        }

    return _write_and_commit("services", id, after, f"chore: retire service {id} via Atlas Write API")


@mcp.tool()
def set_maintenance(id: str, hours: float, reason: str, confirm: bool = False) -> dict[str, Any]:
    """Declare a maintenance window on a service.

    Gate 1.1 (2026-07-27): persona restart/alert authority derives from entity
    STATE, not a static allowlist. While the window is active (unexpired),
    probe_runner's _apply_maintenance() passes every probe for this service --
    no drift, no Aegis service_unhealthy -- and anything consulting
    schemas.service.maintenance_active() must treat the entity as unlocked for
    restart. `until` = now + hours (UTC), computed here so callers declare a
    duration, not a timestamp.

    The window auto-expires at read time once `until` passes -- no cleanup job
    needed. To end it early, call clear_maintenance().

    Returns a before/after preview when confirm=False (default). Call again
    with confirm=True and the same args to apply.
    """
    store = _get_store()
    services = store.get("service", {})
    if id not in services:
        raise ValueError(f"Service '{id}' not found. Known ids: {sorted(services.keys())}")
    if hours <= 0:
        raise ValueError("hours must be > 0")
    if not reason.strip():
        raise ValueError("reason is required")

    rel_path = f"entities/services/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    now = datetime.now(timezone.utc)
    until = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    declared_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    after = dict(current_data)
    after["maintenance"] = {
        "active": True,
        "until": until,
        "reason": reason.strip(),
        "declared_at": declared_at,
    }
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data.get("maintenance"),
            "after": after["maintenance"],
            "note": f"Call set_maintenance(id={id!r}, hours={hours}, reason={reason!r}, confirm=True) to apply.",
        }

    result = _write_and_commit("services", id, after, f"chore: declare maintenance on {id} via Atlas Write API")
    if "error" not in result:
        result["maintenance"] = after["maintenance"]
    return result


@mcp.tool()
def clear_maintenance(id: str, confirm: bool = False) -> dict[str, Any]:
    """End a service's declared maintenance window early (sets active: false).

    Normal auto-expiry (until passing) needs no action at all -- this is only
    for ending a window before `until` arrives.

    Returns a preview when confirm=False (default). Call again with
    confirm=True to apply.
    """
    store = _get_store()
    services = store.get("service", {})
    if id not in services:
        raise ValueError(f"Service '{id}' not found. Known ids: {sorted(services.keys())}")

    rel_path = f"entities/services/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}

    current_maintenance = current_data.get("maintenance")
    after = dict(current_data)
    after["maintenance"] = {**current_maintenance, "active": False} if current_maintenance else {"active": False}
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_maintenance,
            "after": after["maintenance"],
            "note": f"Call clear_maintenance(id={id!r}, confirm=True) to apply.",
        }

    return _write_and_commit("services", id, after, f"chore: clear maintenance on {id} via Atlas Write API")


@mcp.tool()
def add_task(
    project: str,
    title: str,
    next_action: str,
    closure_test: str,
    status: str = "open",
    priority: str = "medium",
    task_type: str = "general",
    why_now: str = "",
    owner_lane: str = "",
    blocked_on: str = "",
    blocked_by_task_ids: list[str] | None = None,
    source: str = "manual",
    source_ref: str = "",
    source_request_id: str = "",
    due_date: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a canonical task entity. Project can be id or exact project name."""
    if status not in _TASK_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_TASK_STATUS_VALUES)}")
    if priority not in _TASK_PRIORITY_VALUES:
        raise ValueError(f"priority must be one of: {sorted(_TASK_PRIORITY_VALUES)}")

    project_id = _resolve_project_id(project)
    normalized_source = _slugify_token(source, fallback="manual")

    store = _get_store()
    tasks = store.get("task", {})
    if source_request_id:
        for existing_task in tasks.values():
            payload = _model_to_dict(existing_task)
            if (
                payload.get("project_id") == project_id
                and payload.get("source") == normalized_source
                and payload.get("source_request_id") == source_request_id
            ):
                return {
                    "ok": True,
                    "idempotent": True,
                    "task_id": payload.get("id"),
                    "project_id": project_id,
                    "task": payload,
                }

    task_id = _next_task_id(project_id, title, normalized_source, source_request_id)
    now = _now_iso()

    data: dict[str, Any] = {
        "id": task_id,
        "project_id": project_id,
        "title": title,
        "status": status,
        "priority": priority,
        "task_type": task_type,
        "next_action": next_action,
        "closure_test": closure_test,
        "source": normalized_source,
        "created_at": now,
        "updated_at": now,
    }
    if why_now:
        data["why_now"] = why_now
    if owner_lane:
        data["owner_lane"] = owner_lane
    if blocked_on:
        data["blocked_on"] = blocked_on
    if blocked_by_task_ids:
        data["blocked_by_task_ids"] = blocked_by_task_ids
    if source_ref:
        data["source_ref"] = source_ref
    if source_request_id:
        data["source_request_id"] = source_request_id
    if due_date:
        data["due_date"] = due_date
    if notes:
        data["notes"] = notes
    if status == "resolved":
        data["resolved_at"] = now

    result = _write_and_commit("tasks", task_id, data, f"feat: add task {task_id} via Atlas Write API")
    if "error" in result:
        return result
    return {
        "ok": True,
        "idempotent": False,
        "task_id": task_id,
        "project_id": project_id,
        **result,
    }


@mcp.tool()
def talos_queue_build(goal: str, repo: str, closure_test: str,
                      constraints: list[str] | None = None, title: str = "") -> dict[str, Any]:
    """Queue an autonomous build for Talos, the dictate-to-build executor.

    Talos is a systemd watcher on the host that polls Atlas project 'talos'
    every minute for open build-intent tasks, then runs headless Claude Code
    (claude -p, flat-rate Max OAuth) in the target repo, commits + pushes the
    result, writes the commit SHA back to this task, and pings Discord. Use this
    to hand Talos a self-contained coding task to do unattended.

    Provide:
      goal:         what to build/change (the concrete outcome).
      repo:         absolute target repo path. Talos operates default-allow
                    inside /opt/stack/services/projects (protected existing
                    projects are excluded); a new path under there is created.
      closure_test: how to know it's done (load-bearing — an intent that can't
                    state done is underspecified and bounces back).
      constraints:  list of hard constraints (may be empty).
      title:        optional short title; derived from goal if omitted.

    Underspecified or ambiguous tasks are bounced to needs-clarification with a
    Discord ping rather than guessed at. Returns the created task id.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string describing what to build.")
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-empty string (absolute target repo path).")
    if not isinstance(closure_test, str) or not closure_test.strip():
        raise ValueError("closure_test must be a non-empty string (how to know it's done).")

    constraints = constraints or []
    if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
        raise ValueError("constraints must be a list of strings.")

    title = title.strip() if isinstance(title, str) else ""
    if not title:
        title = goal.strip()[:60]

    project_id = _resolve_project_id("talos")
    task_id = _next_task_id(project_id, title, "manual", "")
    now = _now_iso()

    notes = (
        "Queued via talos_queue_build (Talos MCP front door).\n\n"
        "```intent\n" + json.dumps({"repo": repo, "constraints": constraints}) + "\n```\n"
    )

    data: dict[str, Any] = {
        "id": task_id,
        "project_id": project_id,
        "title": title,
        "status": "open",
        "priority": "high",
        "task_type": "build-intent",
        "next_action": goal,
        "closure_test": closure_test,
        "source": "manual",
        "created_at": now,
        "updated_at": now,
        "notes": notes,
    }

    result = _write_and_commit("tasks", task_id, data, f"feat: queue talos build {task_id}")
    if "error" in result:
        return result
    return {"ok": True, "task_id": task_id, "project_id": project_id, **result}


# ---------------------------------------------------------------------------
# Talos build lifecycle tools (status / list / cancel / requeue).
# All operate only on project 'talos' tasks with task_type == 'build-intent'.
# Talos is a systemd watcher that autonomously runs `claude -p` on any OPEN
# build-intent task in project 'talos' — so flipping a task to 'open' (requeue)
# hands it to an unattended build on the next poll (within a minute).
# ---------------------------------------------------------------------------

_TALOS_OUTCOME_VALUES = {"needs-review", "build-failed", "needs-clarification"}


def _load_talos_build_task(task_id: str) -> dict[str, Any]:
    """Load a Talos build-intent task as a plain dict.

    Raises ValueError if the id doesn't exist or the task isn't a Talos build
    task (project_id == 'talos' and task_type == 'build-intent')."""
    store = _get_store()
    tasks = store.get("task", {})
    task = tasks.get(task_id)
    if task is None:
        raise ValueError(f"Talos build '{task_id}' not found.")
    payload = _model_to_dict(task)
    if payload.get("project_id") != "talos" or payload.get("task_type") != "build-intent":
        raise ValueError(
            f"Task '{task_id}' is not a Talos build "
            f"(project_id={payload.get('project_id')!r}, task_type={payload.get('task_type')!r})."
        )
    return payload


@mcp.tool()
def talos_status(task_id: str) -> dict[str, Any]:
    """Get the current state of a Talos build: status, outcome (needs-review with
    commit SHA / build-failed / needs-clarification), and the disposition notes."""
    payload = _load_talos_build_task(task_id)
    return {
        "task_id": task_id,
        "title": payload.get("title"),
        "status": payload.get("status"),
        "outcome": payload.get("blocked_on") or None,
        "goal": payload.get("next_action"),
        "closure_test": payload.get("closure_test"),
        "updated_at": payload.get("updated_at"),
        "notes": payload.get("notes"),
    }


@mcp.tool()
def talos_list_builds(limit: int = 20, state: str = "") -> dict[str, Any]:
    """List recent Talos builds, newest first, optionally filtered by state
    (open, in_progress, needs-review, build-failed, needs-clarification,
    deferred, resolved)."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    store = _get_store()
    tasks = store.get("task", {})
    builds: list[tuple[str, dict[str, Any]]] = []
    for task_id, task in tasks.items():
        payload = _model_to_dict(task)
        if payload.get("project_id") != "talos" or payload.get("task_type") != "build-intent":
            continue
        builds.append((task_id, payload))

    if state:
        if state in _TASK_STATUS_VALUES:
            builds = [b for b in builds if b[1].get("status") == state]
        elif state in _TALOS_OUTCOME_VALUES:
            builds = [b for b in builds if b[1].get("blocked_on") == state]
        else:
            allowed = sorted(_TASK_STATUS_VALUES | _TALOS_OUTCOME_VALUES)
            raise ValueError(f"state must be empty or one of: {allowed}")

    builds.sort(
        key=lambda item: item[1].get("updated_at") or item[1].get("created_at"),
        reverse=True,
    )
    builds = builds[:limit]

    return {
        "count": len(builds),
        "builds": [
            {
                "id": task_id,
                "title": payload.get("title"),
                "status": payload.get("status"),
                "outcome": payload.get("blocked_on") or None,
                "updated_at": payload.get("updated_at"),
            }
            for task_id, payload in builds
        ],
    }


@mcp.tool()
def talos_cancel(task_id: str) -> dict[str, Any]:
    """Cancel a queued (not-yet-started) Talos build by deferring it so the
    watcher skips it."""
    payload = _load_talos_build_task(task_id)
    status = payload.get("status")
    if status != "open":
        blocked_on = payload.get("blocked_on")
        reason = f"only a queued (open) build can be cancelled; this task is {status}"
        if blocked_on:
            reason += f"/{blocked_on}"
        return {"ok": False, "task_id": task_id, "reason": reason}

    rel_path = f"entities/tasks/{task_id}.yaml"
    abs_path = REPO_ROOT / rel_path
    after: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    now = _now_iso()
    existing_notes = after.get("notes") or ""
    after["notes"] = (
        existing_notes
        + f"\n\n---\n[talos-cancel {now}] build cancelled by operator; Talos will not run it."
    )
    after["status"] = "deferred"
    after.pop("resolved_at", None)
    after["updated_at"] = now

    result = _write_and_commit("tasks", task_id, after, f"chore: cancel talos build {task_id}")
    if "error" in result:
        return result
    return {"ok": True, "task_id": task_id, "status": "deferred"}


@mcp.tool()
def talos_requeue(task_id: str, note: str = "") -> dict[str, Any]:
    """Re-queue a finished or cancelled Talos build (e.g. after adding the missing
    clarification the ping asked for) so the watcher runs it again. Preserves the
    original intent; append a note describing what changed."""
    payload = _load_talos_build_task(task_id)
    status = payload.get("status")
    if status == "in_progress":
        return {
            "ok": False,
            "task_id": task_id,
            "reason": "build is currently running; wait for it to finish before requeuing",
        }

    rel_path = f"entities/tasks/{task_id}.yaml"
    abs_path = REPO_ROOT / rel_path
    after: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    now = _now_iso()
    existing_notes = after.get("notes") or ""
    line = f"\n\n---\n[talos-requeue {now}] re-queued for Talos"
    if note:
        line += f": {note}"
    after["notes"] = existing_notes + line
    after["status"] = "open"
    after["blocked_on"] = ""
    after.pop("resolved_at", None)
    after["updated_at"] = now

    result = _write_and_commit("tasks", task_id, after, f"chore: requeue talos build {task_id}")
    if "error" in result:
        return result
    return {
        "ok": True,
        "task_id": task_id,
        "status": "open",
        "note": "Talos will pick it up on the next cycle (within a minute)",
    }


@mcp.tool()
def update_task(
    id: str,
    confirm: bool = False,
    status: str = "",
    priority: str = "",
    title: str = "",
    next_action: str = "",
    closure_test: str = "",
    owner_lane: str = "",
    blocked_on: str = "",
    notes: str = "",
    due_date: str = "",
) -> dict[str, Any]:
    """Update fields on an existing task entity via propose-confirm pattern."""
    store = _get_store()
    tasks = store.get("task", {})
    if id not in tasks:
        raise ValueError(f"Task '{id}' not found. Known ids: {sorted(tasks.keys())}")

    rel_path = f"entities/tasks/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if status:
        if status not in _TASK_STATUS_VALUES:
            raise ValueError(f"status must be one of: {sorted(_TASK_STATUS_VALUES)}")
        after["status"] = status
    if priority:
        if priority not in _TASK_PRIORITY_VALUES:
            raise ValueError(f"priority must be one of: {sorted(_TASK_PRIORITY_VALUES)}")
        after["priority"] = priority
    if title:
        after["title"] = title
    if next_action:
        after["next_action"] = next_action
    if closure_test:
        after["closure_test"] = closure_test
    if owner_lane:
        after["owner_lane"] = owner_lane
    if blocked_on:
        after["blocked_on"] = blocked_on
    if notes:
        after["notes"] = notes
    if due_date:
        after["due_date"] = due_date

    effective_status = after.get("status", "open")
    if effective_status == "resolved":
        after["resolved_at"] = _now_iso()
    else:
        after.pop("resolved_at", None)

    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_task(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("tasks", id, after, f"chore: update task {id} via Atlas Write API")


# ---------------------------------------------------------------------------
# Consolidated memories + trails (a consolidation loop).
#
# ---------------------------------------------------------------------------

@mcp.tool()
def list_memories(memory_type: str = "", status: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List consolidated memory entities. Optional filters: memory_type
    (identity|preference|expertise|decision|reference), status (active|superseded)."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if memory_type and memory_type not in _MEMORY_TYPE_VALUES:
        raise ValueError(f"memory_type must be one of: {sorted(_MEMORY_TYPE_VALUES)}")
    if status and status not in _MEMORY_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_MEMORY_STATUS_VALUES)}")

    store = _get_store()
    memories = store.get("memory", {})
    result: list[dict[str, Any]] = []
    for memory_id, memory in sorted(memories.items()):
        payload = _model_to_dict(memory)
        mt = payload.get("memory_type")
        mt_value = mt.get("value_id") if isinstance(mt, dict) else (str(mt).split(":")[-1] if mt else "")
        if memory_type and mt_value != memory_type:
            continue
        if status and payload.get("status") != status:
            continue
        result.append(
            {
                "id": memory_id,
                "memory_type": mt_value,
                "statement": payload.get("statement"),
                "confidence": payload.get("confidence"),
                "status": payload.get("status"),
                "recurrence_sessions": payload.get("recurrence_sessions"),
                "updated_at": payload.get("updated_at"),
            }
        )
        if len(result) >= limit:
            break
    return result


@mcp.tool()
def get_memory(id: str) -> dict[str, Any]:
    """Return the full consolidated memory entity for the given id."""
    store = _get_store()
    memories = store.get("memory", {})
    memory = memories.get(id)
    if memory is None:
        raise ValueError(f"Memory '{id}' not found. Known ids: {sorted(memories.keys())}")
    return _model_to_dict(memory)


@mcp.tool()
def list_trails(status: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List trail (exploratory lead) entities. Optional filter: status
    (open|pulled|led-somewhere|dead)."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if status and status not in _TRAIL_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_TRAIL_STATUS_VALUES)}")

    store = _get_store()
    trails = store.get("trail", {})
    result: list[dict[str, Any]] = []
    for trail_id, trail in sorted(trails.items()):
        payload = _model_to_dict(trail)
        st = payload.get("status")
        st_value = st.get("value_id") if isinstance(st, dict) else (str(st).split(":")[-1] if st else "")
        if status and st_value != status:
            continue
        result.append(
            {
                "id": trail_id,
                "title": payload.get("title"),
                "status": st_value,
                "score": payload.get("score"),
                "question": payload.get("question"),
                "updated_at": payload.get("updated_at"),
            }
        )
        if len(result) >= limit:
            break
    return result


@mcp.tool()
def get_trail(id: str) -> dict[str, Any]:
    """Return the full trail entity for the given id."""
    store = _get_store()
    trails = store.get("trail", {})
    trail = trails.get(id)
    if trail is None:
        raise ValueError(f"Trail '{id}' not found. Known ids: {sorted(trails.keys())}")
    return _model_to_dict(trail)


@mcp.tool()
def bootstrap_tasks_from_projects(project: str = "", confirm: bool = False, limit: int = 500) -> dict[str, Any]:
    """Backfill canonical tasks from project next_action and open_items fields.

    project: optional project id or exact name to scope bootstrap
    confirm: preview by default; set True to write tasks
    """
    if limit < 1 or limit > 2000:
        raise ValueError("limit must be between 1 and 2000")

    target_project_id = _resolve_project_id(project) if project else ""

    store = _get_store()
    projects = store.get("project", {})
    task_store = store.get("task", {})

    existing_refs: set[str] = set()
    for task_model in task_store.values():
        payload = _model_to_dict(task_model)
        source_ref = payload.get("source_ref")
        if isinstance(source_ref, str) and source_ref:
            existing_refs.add(source_ref)

    proposals: list[dict[str, Any]] = []
    for project_id, project_model in sorted(projects.items()):
        if target_project_id and project_id != target_project_id:
            continue

        payload = _model_to_dict(project_model)
        status_value = str(payload.get("status", {}).get("value_id", "")).strip()
        next_action = str(payload.get("next_action") or "").strip()
        open_items = payload.get("open_items", []) or []
        if next_action:
            source_ref = f"project:{project_id}:next_action"
            if source_ref not in existing_refs:
                proposals.append(
                    {
                        "project_id": project_id,
                        "title": f"Bootstrap next action for {project_id}",
                        "next_action": next_action,
                        "closure_test": "Execution evidence captured and project next_action updated.",
                        "priority": "medium",
                        "status": "open",
                        "task_type": "migration",
                        "source": "bootstrap",
                        "source_ref": source_ref,
                    }
                )

        for item in open_items:
            item_id = str(item.get("id") or "").strip()
            item_desc = str(item.get("description") or "").strip()
            if not item_id or not item_desc:
                continue
            source_ref = f"project:{project_id}:open_item:{item_id}"
            if source_ref in existing_refs:
                continue
            proposals.append(
                {
                    "project_id": project_id,
                    "title": f"Bootstrap open item {item_id}",
                    "next_action": item_desc,
                    "closure_test": "Open item resolved and removed from project.open_items.",
                    "priority": "high",
                    "status": "open",
                    "task_type": "migration",
                    "source": "bootstrap",
                    "source_ref": source_ref,
                }
            )

        # Seed a minimum tracking task when an active/in_progress project has
        # no canonical next_action and no open items to bootstrap from.
        if status_value in {"active", "in_progress"} and not next_action and not open_items:
            source_ref = f"project:{project_id}:task_tracking_gap"
            if source_ref not in existing_refs:
                proposals.append(
                    {
                        "project_id": project_id,
                        "title": f"Bootstrap tracking gap for {project_id}",
                        "next_action": (
                            "Define the immediate next action for this project and "
                            "decompose follow-on work into canonical tasks."
                        ),
                        "closure_test": (
                            "Project has a current next_action and at least one "
                            "maintained canonical task."
                        ),
                        "priority": "medium",
                        "status": "open",
                        "task_type": "tracking_gap",
                        "source": "bootstrap",
                        "source_ref": source_ref,
                    }
                )

        if len(proposals) >= limit:
            break

    preview = {
        "action": "preview" if not confirm else "apply",
        "count": len(proposals),
        "sample": proposals[:20],
    }
    if not confirm:
        preview["note"] = "Call bootstrap_tasks_from_projects(confirm=True) to apply."
        return preview

    created: list[str] = []
    errors: list[dict[str, Any]] = []
    for proposal in proposals:
        task_id = _next_task_id(
            proposal["project_id"],
            proposal["title"],
            proposal["source"],
            proposal["source_ref"],
        )
        now = _now_iso()
        data = {
            "id": task_id,
            "project_id": proposal["project_id"],
            "title": proposal["title"],
            "status": proposal["status"],
            "priority": proposal["priority"],
            "task_type": proposal["task_type"],
            "next_action": proposal["next_action"],
            "closure_test": proposal["closure_test"],
            "source": proposal["source"],
            "source_ref": proposal["source_ref"],
            "created_at": now,
            "updated_at": now,
        }
        result = _write_and_commit("tasks", task_id, data, f"feat: bootstrap task {task_id} via Atlas Write API")
        if "error" in result:
            errors.append({"task_id": task_id, "error": result["error"]})
        else:
            created.append(task_id)

    return {
        "action": "apply",
        "requested": len(proposals),
        "created": len(created),
        "failed": len(errors),
        "created_task_ids": created,
        "errors": errors,
    }


@mcp.tool()
def sync_project_next_actions_from_tasks(project: str = "", confirm: bool = False, limit: int = 200) -> dict[str, Any]:
    """Backfill empty project next_action values from canonical open tasks.

    Selects the most recently updated open task per project and copies its
    next_action into the project entity when project next_action is empty.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    target_project_id = _resolve_project_id(project) if project else ""
    store = _get_store()
    projects = store.get("project", {})
    tasks = store.get("task", {})

    open_states = {"open", "in_progress", "blocked"}
    tasks_by_project: dict[str, list[dict[str, Any]]] = {}
    for task_model in tasks.values():
        payload = _model_to_dict(task_model)
        if payload.get("status") not in open_states:
            continue
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            continue
        tasks_by_project.setdefault(project_id, []).append(payload)

    proposals: list[dict[str, str]] = []
    for project_id, project_model in sorted(projects.items()):
        if target_project_id and project_id != target_project_id:
            continue
        payload = _model_to_dict(project_model)
        status_value = str(payload.get("status", {}).get("value_id", "")).strip()
        if status_value not in {"active", "in_progress"}:
            continue

        current_next = str(payload.get("next_action") or "").strip()
        if current_next:
            continue

        project_tasks = tasks_by_project.get(project_id, [])
        if not project_tasks:
            continue

        project_tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        chosen = project_tasks[0]
        chosen_next = str(chosen.get("next_action") or "").strip()
        if not chosen_next:
            continue

        proposals.append(
            {
                "project_id": project_id,
                "task_id": str(chosen.get("id") or ""),
                "next_action": chosen_next,
            }
        )
        if len(proposals) >= limit:
            break

    if not confirm:
        return {
            "action": "preview",
            "count": len(proposals),
            "sample": proposals[:20],
            "note": "Call sync_project_next_actions_from_tasks(confirm=True) to apply.",
        }

    updated: list[str] = []
    errors: list[dict[str, Any]] = []
    for proposal in proposals:
        project_id = proposal["project_id"]
        rel_path = f"entities/projects/{project_id}.yaml"
        abs_path = REPO_ROOT / rel_path
        current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
        current_data["next_action"] = proposal["next_action"]
        current_data["updated_at"] = _now_iso()
        result = _write_and_commit(
            "projects",
            project_id,
            current_data,
            f"chore: sync project next_action for {project_id} from task {proposal['task_id']}",
        )
        if "error" in result:
            errors.append({"project_id": project_id, "error": result["error"]})
        else:
            updated.append(project_id)

    return {
        "action": "apply",
        "requested": len(proposals),
        "updated": len(updated),
        "failed": len(errors),
        "updated_projects": updated,
        "errors": errors,
    }


DEFAULT_OUTPUTS_DIR = REPO_ROOT / "outputs"
DEFAULT_KB_DOC_ROOT = Path("/opt/stack/services/docs/kb")
DEFAULT_KB_OUTPUT_DIR = DEFAULT_KB_DOC_ROOT / "Projects" / "Atlas" / "40-OUTPUT"
DEFAULT_SERVICES_REPO_ROOT = Path("/opt/stack/services")
KB_ROOT_ALLOWED_FILES = {
    "Start Here.md",
    "Standards.md",
    "Project Index.md",
    "The Latest.md",
    "System Log.md",
    "How I Work.md",
}


def _services_repo_root() -> Path:
    return Path(os.environ.get("ATLAS_SERVICES_REPO_ROOT", str(DEFAULT_SERVICES_REPO_ROOT))).resolve()


def _kb_doc_root() -> Path:
    return Path(os.environ.get("ATLAS_KB_DOC_ROOT", str(DEFAULT_KB_DOC_ROOT))).resolve()


def _is_kb_stage_name(stage_name: str) -> bool:
    return bool(re.match(r"^\d{2}-[A-Z0-9-]+$", stage_name))


def _resolve_kb_rel_path(path: str) -> tuple[Path, Path]:
    if not path or not path.strip():
        raise ValueError("Path is required")

    rel = Path(path.strip())
    if rel.is_absolute():
        raise ValueError("Absolute paths are not allowed")
    if any(part == ".." for part in rel.parts):
        raise ValueError("Path traversal is not allowed")

    root = _kb_doc_root()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError(f"Invalid doc path: {path!r}")
    return rel, target


def _is_allowed_kb_write(rel: Path) -> bool:
    if rel.suffix.lower() != ".md":
        return False

    parts = rel.parts
    if len(parts) == 1 and parts[0] in KB_ROOT_ALLOWED_FILES:
        return True

    if len(parts) < 4:
        return False
    if parts[0] != "Projects":
        return False
    if not _is_kb_stage_name(parts[2]):
        return False

    return True


def _sha256_text(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=".tmp-",
        suffix=".md",
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)


def _git_commit_paths(repo_root: Path, rel_paths: list[str], message: str) -> dict[str, Any]:
    """Stage and commit specific files in ``repo_root`` (the SERVICES repo for KB docs).

    Race-safety (Tier 1 pattern, mirrors ``_git_commit`` for the atlas-store repo):
      1. Held under ``atlas_write_lock(repo_root)`` — an advisory flock scoped to
         *this* repo's ``.git`` (services/.git/atlas-write.lock), serializing
         atlas-mcp's own concurrent KB writers so they can't collide on index.lock.
      2. Both ``git add`` and ``git commit`` carry an explicit ``--`` pathspec, so a
         concurrent agent's pre-staged (but unrelated) files in the services index
         are NEVER swept into this commit and remain staged/untouched afterward.
         Previously the commit ran with no pathspec (``git commit -m msg``), which
         captured the whole index — the live sweep bug this closes.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "atlas-mcp",
        "GIT_COMMITTER_NAME": "atlas-mcp",
        "GIT_AUTHOR_EMAIL": "atlas-mcp@atlas-instance.local",
        "GIT_COMMITTER_EMAIL": "atlas-mcp@atlas-instance.local",
    }

    with atlas_write_lock(repo_root):
        for rel_path in rel_paths:
            add = subprocess.run(
                ["git", "add", "--", rel_path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if add.returncode != 0:
                return {"error": f"git add failed for {rel_path}: {add.stderr.strip() or add.stdout.strip()}"}

        # Scope the "nothing to commit" check to OUR paths only — a concurrent
        # agent's staged files must not make us think there's work to do (nor be
        # picked up if there is).
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", *rel_paths],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff.returncode == 0:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return {
                "ok": True,
                "sha": head.stdout.strip() if head.returncode == 0 else "",
                "committed": False,
            }

        commit = subprocess.run(
            ["git", "commit", "-m", message, "--", *rel_paths],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if commit.returncode != 0:
            return {"error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"}

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "ok": True,
            "sha": head.stdout.strip() if head.returncode == 0 else "",
            "committed": True,
        }


@mcp.tool()
def get_kb_doc(name: str) -> str:
    """Read a knowledge base document from the configured KB doc root.

    Pass a relative path such as 'Start Here.md', 'Standards.md', or
    'Projects/atlas/10-CONCEPT/ATLAS_CONCEPT.md'. Returns the file content.
    """
    kb_root = Path(os.environ.get("ATLAS_KB_DOC_ROOT", str(DEFAULT_KB_DOC_ROOT)))
    target = (kb_root / name).resolve()
    # Guard against path traversal
    if not str(target).startswith(str(kb_root.resolve())):
        raise ValueError(f"Invalid doc name: {name!r}")
    if not target.exists():
        raise FileNotFoundError(
            f"KB doc '{name}' not found under {kb_root}. "
            "This KB root has no such document yet — create it via create_kb_doc "
            "or check the path with a directory-level doc listing."
        )
    return target.read_text(encoding="utf-8")


# --- KB degenerate-payload guard (create_kb_doc / update_kb_doc) -------------
# Both tools take the ENTIRE document as `content`, so a payload that was never
# real content destroys the doc on confirm. Two near-misses in the week of
# 2026-07-27, both self-caught by the calling agent and neither guarded against:
# a literal unexpanded "$(cat ...)" string, and a "__SAME_AS_ABOVE__" token.
# Same posture as the H1 whole-file guard in _kb_replace_section_text: refuse the
# obviously-degenerate shapes, keep each check narrow so real writes are
# untouched (fenced/inline-code occurrences are exempt — a doc that QUOTES these
# shapes is fine), and give the one occasionally-intended shape (a much smaller
# replacement doc) an explicit allow_shrink flag on update_kb_doc.

_KB_SUBSTITUTION_PAYLOAD_RE = re.compile(r"^(?:\$\(.+\)|\$\{.+\}|`.+`)$", re.DOTALL)
_KB_CAT_SUBSTITUTION_RE = re.compile(r"\$\((?:cat|<)[ \t]")
_KB_PLACEHOLDER_TOKEN_RE = re.compile(r"\b__[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+__\b")
_KB_PLACEHOLDER_LINE_RE = re.compile(r"^__[A-Z][A-Z0-9_]*__$")
_KB_INLINE_CODE_RE = re.compile(r"`[^`]*`")

_KB_SHRINK_MIN_CHARS = 800   # existing docs smaller than this may shrink freely
_KB_SHRINK_RATIO = 3         # refuse when new content < old/3 without allow_shrink


def _kb_prose_lines(content: str):
    """Yield (line_no, line) for lines outside fenced code blocks, with inline
    code spans stripped — the degenerate-payload checks only read prose, so a
    doc that legitimately quotes `$(cat ...)` or `__A_TOKEN__` passes."""
    fence_char, fence_len = "", 0
    for i, line in enumerate(content.split("\n"), start=1):
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            continue
        if fence_char:
            continue
        yield i, _KB_INLINE_CODE_RE.sub("", line)


def _kb_reject_degenerate_content(content: str, operation: str) -> None:
    """Refuse payloads that are obviously not the document they claim to be:
    empty/whitespace bodies, unexpanded shell substitutions, placeholder tokens.
    Raises ValueError (both at preview and confirm, so callers learn early)."""
    stripped = content.strip()
    if not stripped:
        raise ValueError(
            f"Refusing {operation}: content is empty/whitespace — that is a doc wipe, not a "
            "write. Pass the full document text; there is no sanctioned KB delete path."
        )
    if _KB_SUBSTITUTION_PAYLOAD_RE.match(stripped):
        raise ValueError(
            f"Refusing {operation}: content is a single unexpanded shell substitution "
            f"({stripped[:80]!r}). MCP arguments are never shell-expanded — read the file "
            "yourself and pass its literal text as content."
        )
    for line_no, prose in _kb_prose_lines(content):
        if _KB_CAT_SUBSTITUTION_RE.search(prose):
            raise ValueError(
                f"Refusing {operation}: line {line_no} contains an unexpanded shell file "
                "substitution ('$(cat ...' / '$(< ...'). MCP arguments are never "
                "shell-expanded; pass the literal text. If the doc genuinely quotes this "
                "syntax, put it in inline code or a fenced block."
            )
        placeholder = None
        m = _KB_PLACEHOLDER_TOKEN_RE.search(prose)
        if m:
            placeholder = m.group(0)
        elif _KB_PLACEHOLDER_LINE_RE.match(prose.strip()):
            placeholder = prose.strip()
        if placeholder:
            raise ValueError(
                f"Refusing {operation}: line {line_no} carries the placeholder token "
                f"{placeholder!r} — the payload was never fully written out. Expand it to "
                "the real text, or wrap it in inline code/a fenced block if the doc "
                "genuinely documents that token."
            )


def _kb_reject_unacknowledged_shrink(existing: str, content: str, allow_shrink: bool) -> None:
    """Refuse a full-file overwrite dramatically smaller than the existing doc
    unless allow_shrink=True — the 2026-07-26 incident shape (394 lines -> 26)
    reached the tree because nothing measured the payload against the doc."""
    old_len = len(existing.strip())
    new_len = len(content.strip())
    if allow_shrink or old_len < _KB_SHRINK_MIN_CHARS or new_len * _KB_SHRINK_RATIO >= old_len:
        return
    pct = round(100.0 * new_len / old_len, 1) if old_len else 0.0
    raise ValueError(
        f"Refusing update_kb_doc: replacement content is {new_len} chars against an existing "
        f"{old_len}-char doc ({pct}% of the original). update_kb_doc is a FULL-FILE "
        "overwrite; a payload this much smaller is usually a truncated or partial document. "
        "Use replace_kb_section/append_kb_doc for partial edits, or pass allow_shrink=True "
        "if replacing the doc with a much shorter one is intended."
    )


@mcp.tool()
def create_kb_doc(path: str, content: str, confirm: bool = False, commit: bool = True) -> dict[str, Any]:
    """Create a markdown document under the configured KB doc root via Atlas.

    Single sanctioned KB write path. Uses propose-confirm by default.
    Set confirm=True to apply the write. Degenerate payloads (empty/whitespace,
    unexpanded $(...) substitutions, __PLACEHOLDER__ tokens) are refused.
    """
    rel, target = _resolve_kb_rel_path(path)
    if not _is_allowed_kb_write(rel):
        raise ValueError(
            "KB write path is not allowed by policy. "
            "Allowed: root canonical docs or Projects/<Project>/<NN-STAGE>/<file>.md"
        )
    _kb_reject_degenerate_content(content, "create_kb_doc")

    exists = target.exists()
    after_hash = _sha256_text(content)

    if not confirm:
        return {
            "action": "preview",
            "operation": "create_kb_doc",
            "path": rel.as_posix(),
            "exists": exists,
            "after_hash": after_hash,
            "commit": commit,
            "note": "Call create_kb_doc(..., confirm=True) to apply.",
        }

    if exists:
        raise FileExistsError(f"KB doc already exists: {rel.as_posix()}")

    _atomic_write_text(target, content)
    file_hash = _sha256_file(target)

    commit_sha = ""
    committed = False
    if commit:
        repo_root = _services_repo_root()
        try:
            repo_rel = str(target.resolve().relative_to(repo_root.resolve()))
        except ValueError as exc:
            raise ValueError(f"KB doc path is outside repo root {repo_root}: {target}") from exc

        result = _git_commit_paths(repo_root, [repo_rel], f"docs: create {rel.as_posix()} via Atlas KB Write API")
        if "error" in result:
            return {
                "status": "error",
                "path": rel.as_posix(),
                "file_hash": file_hash,
                "commit_sha": "",
                "error": result["error"],
            }
        commit_sha = result.get("sha", "")
        committed = bool(result.get("committed", False))

    return {
        "status": "ok",
        "operation": "create_kb_doc",
        "path": rel.as_posix(),
        "file_hash": file_hash,
        "commit_sha": commit_sha,
        "committed": committed,
    }


@mcp.tool()
def update_kb_doc(path: str, content: str, confirm: bool = False, commit: bool = True,
                  allow_shrink: bool = False) -> dict[str, Any]:
    """Update a markdown document under the configured KB doc root via Atlas.

    Single sanctioned KB write path. Uses propose-confirm by default.
    Set confirm=True to apply the write. This is a FULL-FILE overwrite:
    degenerate payloads (empty/whitespace, unexpanded $(...) substitutions,
    __PLACEHOLDER__ tokens) are refused, and content dramatically smaller than
    the existing doc is refused unless allow_shrink=True. For partial edits
    prefer replace_kb_section/append_kb_doc.
    """
    rel, target = _resolve_kb_rel_path(path)
    if not _is_allowed_kb_write(rel):
        raise ValueError(
            "KB write path is not allowed by policy. "
            "Allowed: root canonical docs or Projects/<Project>/<NN-STAGE>/<file>.md"
        )

    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"KB doc not found: {rel.as_posix()}")

    _kb_reject_degenerate_content(content, "update_kb_doc")
    _kb_reject_unacknowledged_shrink(target.read_text(encoding="utf-8"), content, allow_shrink)

    before_hash = _sha256_file(target)
    after_hash = _sha256_text(content)

    if not confirm:
        return {
            "action": "preview",
            "operation": "update_kb_doc",
            "path": rel.as_posix(),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "changed": before_hash != after_hash,
            "commit": commit,
            "note": "Call update_kb_doc(..., confirm=True) to apply.",
        }

    _atomic_write_text(target, content)
    file_hash = _sha256_file(target)

    commit_sha = ""
    committed = False
    if commit:
        repo_root = _services_repo_root()
        try:
            repo_rel = str(target.resolve().relative_to(repo_root.resolve()))
        except ValueError as exc:
            raise ValueError(f"KB doc path is outside repo root {repo_root}: {target}") from exc

        result = _git_commit_paths(repo_root, [repo_rel], f"docs: update {rel.as_posix()} via Atlas KB Write API")
        if "error" in result:
            return {
                "status": "error",
                "path": rel.as_posix(),
                "file_hash": file_hash,
                "commit_sha": "",
                "error": result["error"],
            }
        commit_sha = result.get("sha", "")
        committed = bool(result.get("committed", False))

    return {
        "status": "ok",
        "operation": "update_kb_doc",
        "path": rel.as_posix(),
        "file_hash": file_hash,
        "commit_sha": commit_sha,
        "committed": committed,
    }


# --- KB partial-write ops (append / anchored section replace) ---------------
# update_kb_doc is a full-file overwrite: the caller must supply the ENTIRE doc,
# so a truncated payload silently destroys content. The ops below move the
# read-modify-write SERVER-SIDE — the caller supplies only the new text — so
# truncation is structurally impossible. Each runs under a per-doc advisory lock
# so two concurrent splices can't lose an update. (Broader cross-writer
# serialization of update/create/entity paths remains the write-path task.)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


@contextlib.contextmanager
def _kb_doc_lock(target: Path):
    """Exclusive advisory lock serializing read-modify-write on one KB doc.

    Lock files live in a temp dir keyed by the target path (not in the repo, so
    they never show up as untracked). Reusable for any KB write path.
    """
    lock_dir = Path(tempfile.gettempdir()) / "atlas-kb-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()
    lock_fd = os.open(str(lock_dir / f"{key}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _find_md_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (line_index, level, text) for ATX headings, ignoring code fences."""
    headings: list[tuple[int, int, str]] = []
    fence_char = ""
    fence_len = 0
    for i, line in enumerate(lines):
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char, fence_len = "", 0
            continue
        if fence_char:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))
    return headings


def _kb_append_text(text: str, content: str) -> str:
    base = text.rstrip("\n")
    body = content.strip("\n")
    if not body:
        return text if text.endswith("\n") or not text else text + "\n"
    return f"{base}\n\n{body}\n" if base else f"{body}\n"


def _kb_replace_section_text(text: str, anchor: str, new_body: str, create_missing: bool,
                             allow_whole_file: bool = False) -> str:
    """Replace the body of the section whose heading matches `anchor` (heading
    kept, replaced up to the next heading of same-or-shallower depth). Raises
    ValueError on a missing (unless create_missing) or ambiguous anchor, or on a
    whole-file overwrite (H1 anchor running to EOF) unless allow_whole_file."""
    lines = text.split("\n")
    headings = _find_md_headings(lines)
    norm = anchor.lstrip("#").strip()
    matches = [k for k, (_i, _l, t) in enumerate(headings) if t == norm]

    if len(matches) > 1:
        at = [headings[k][0] + 1 for k in matches]
        raise ValueError(f"Anchor {norm!r} matches {len(matches)} headings (lines {at}); make it unique.")

    body = new_body.strip("\n")
    if not matches:
        if not create_missing:
            avail = [t for (_i, _l, t) in headings]
            raise ValueError(
                f"Anchor {norm!r} not found. Available headings: {avail}. "
                "Pass create_missing=True to append it as a new section."
            )
        base = text.rstrip("\n")
        return f"{base}\n\n## {norm}\n\n{body}\n" if base else f"## {norm}\n\n{body}\n"

    hidx, level, _ = headings[matches[0]]
    end = len(lines)
    for (idx2, lvl2, _t) in headings[matches[0] + 1:]:
        if lvl2 <= level:
            end = idx2
            break

    # Whole-file-overwrite guard. The doc's leading H1 (its title) with no
    # same-or-shallower heading after it "owns" everything from the title to EOF,
    # so replacing its body discards the whole document. That is not a section
    # edit, and on 2026-07-26 it silently cut a 394-line KB doc to 26 lines
    # (recovered from git). Deliberately narrow so real section edits still work:
    # H2+ anchors are never affected (their span ends at the next H1/H2), and in a
    # multi-H1 doc a later H1 only owns its own tail, not the file.
    first_h1 = not any(lvl == 1 for (_i2, lvl, _t) in headings[:matches[0]])
    if level == 1 and first_h1 and end == len(lines) and not allow_whole_file:
        swallowed = [t for (_i2, _l2, t) in headings[matches[0] + 1:]]
        detail = f", including subsection(s) {swallowed}" if swallowed else ""
        raise ValueError(
            f"Refusing whole-file overwrite: anchor {norm!r} is an H1 whose section runs to "
            f"end of document (lines {hidx + 1}-{len(lines)} of {len(lines)}{detail}). "
            "Replacing its body would discard the entire document below the title. "
            "Anchor a deeper (H2+) heading to edit one section, use append_kb_doc to add "
            "content, or pass allow_whole_file=True if replacing the whole body is intended."
        )

    new_block = [lines[hidx], "", *body.split("\n"), ""]
    new_lines = lines[:hidx] + new_block + lines[end:]
    return "\n".join(new_lines).rstrip("\n") + "\n"


def _commit_kb_doc(target: Path, rel: Path, verb: str) -> tuple[str, bool, str | None]:
    repo_root = _services_repo_root()
    try:
        repo_rel = str(target.resolve().relative_to(repo_root.resolve()))
    except ValueError as exc:
        raise ValueError(f"KB doc path is outside repo root {repo_root}: {target}") from exc
    result = _git_commit_paths(repo_root, [repo_rel], f"docs: {verb} {rel.as_posix()} via Atlas KB Write API")
    if "error" in result:
        return "", False, result["error"]
    return result.get("sha", ""), bool(result.get("committed", False)), None


def _kb_patch_apply(target: Path, rel: Path, expected_hash: str, commit: bool,
                    verb: str, operation: str, compute) -> dict[str, Any]:
    """Shared confirm-path: lock, re-read, optimistic-check, splice, write, commit."""
    with _kb_doc_lock(target):
        current_text = target.read_text(encoding="utf-8")
        current_hash = _sha256_file(target)
        if expected_hash and expected_hash != current_hash:
            return {
                "status": "error", "operation": operation, "path": rel.as_posix(),
                "error": f"stale: expected_hash {expected_hash} != current {current_hash}",
                "current_hash": current_hash,
            }
        final_text = compute(current_text)
        _atomic_write_text(target, final_text)
        file_hash = _sha256_file(target)
        commit_sha, committed, err = "", False, None
        if commit:
            commit_sha, committed, err = _commit_kb_doc(target, rel, verb)
        if err:
            return {"status": "error", "operation": operation, "path": rel.as_posix(),
                    "file_hash": file_hash, "commit_sha": "", "error": err}
    return {"status": "ok", "operation": operation, "path": rel.as_posix(),
            "file_hash": file_hash, "commit_sha": commit_sha, "committed": committed}


def _kb_patch_preview(rel: Path, target: Path, operation: str, commit: bool, compute) -> dict[str, Any]:
    before_text = target.read_text(encoding="utf-8")
    before_hash = _sha256_file(target)
    after_hash = _sha256_text(compute(before_text))
    return {
        "action": "preview", "operation": operation, "path": rel.as_posix(),
        "before_hash": before_hash, "after_hash": after_hash,
        "changed": before_hash != after_hash, "commit": commit,
        "note": f"Call {operation}(..., confirm=True) to apply.",
    }


def _kb_resolve_existing(path: str) -> tuple[Path, Path]:
    rel, target = _resolve_kb_rel_path(path)
    if not _is_allowed_kb_write(rel):
        raise ValueError(
            "KB write path is not allowed by policy. "
            "Allowed: root canonical docs or Projects/<Project>/<NN-STAGE>/<file>.md"
        )
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"KB doc not found: {rel.as_posix()}")
    return rel, target


@mcp.tool()
def append_kb_doc(path: str, content: str, expected_hash: str = "",
                  confirm: bool = False, commit: bool = True) -> dict[str, Any]:
    """Append text to the end of an existing KB markdown doc, server-side.

    Unlike update_kb_doc (full-file overwrite — caller must resend the entire
    document, risking silent truncation), the caller supplies ONLY the new text;
    the server reads the current file and appends. Truncation is impossible.

    expected_hash: optional optimistic guard — the write aborts if the file's
    current hash differs (it changed since you last read it).
    Propose-confirm: call with confirm=True to apply.
    """
    rel, target = _kb_resolve_existing(path)
    compute = lambda t: _kb_append_text(t, content)
    if not confirm:
        return _kb_patch_preview(rel, target, "append_kb_doc", commit, compute)
    return _kb_patch_apply(target, rel, expected_hash, commit, "append", "append_kb_doc", compute)


@mcp.tool()
def replace_kb_section(path: str, anchor: str, content: str, create_missing: bool = False,
                       expected_hash: str = "", confirm: bool = False, commit: bool = True,
                       allow_whole_file: bool = False) -> dict[str, Any]:
    """Replace one markdown section's body in a KB doc, leaving the rest intact.

    `anchor` is the heading text (with or without leading #). The matched heading
    line is kept; its body is replaced up to the next heading of same-or-shallower
    depth. The caller supplies ONLY the new section body — no full-file round-trip,
    so the rest of the doc cannot be truncated.

    - anchor not found -> error listing available headings, unless
      create_missing=True (then a new `## anchor` section is appended).
    - anchor matches >1 heading -> error with line numbers (disambiguate).
    - anchor is an H1 whose section runs to end of document -> refused, because
      that replaces the whole doc, not a section. Anchor an H2+ heading instead,
      or pass allow_whole_file=True if you really mean to replace everything
      below the title.
    expected_hash: optional optimistic guard (see append_kb_doc).
    Propose-confirm: call with confirm=True to apply.
    """
    rel, target = _kb_resolve_existing(path)
    compute = lambda t: _kb_replace_section_text(t, anchor, content, create_missing, allow_whole_file)
    if not confirm:
        return _kb_patch_preview(rel, target, "replace_kb_section", commit, compute)
    return _kb_patch_apply(target, rel, expected_hash, commit, "update", "replace_kb_section", compute)


@mcp.tool()
def check_drift(service_id: str = "", force: bool = False) -> list[dict[str, Any]]:
    """Run reality probes against running services and report drift.

    By default reads the cached probe result written by the atlas-probe systemd
    timer (automations/state/atlas_probe_latest.json). Set force=True to run
    live probes and refresh the cache.

    Returns a list of per-service probe results. Each result has:
    - service_id, name, lifecycle
    - probes: list of {type, expected, actual, pass}
    - drift: bool — True if any probe failed

    Pass service_id to filter to one service; omit for all non-retired services.
    """
    PROBE_CACHE = Path("/opt/stack/services/automations/state/atlas_probe_latest.json")

    if not force and PROBE_CACHE.exists() and not service_id:
        try:
            import json as _json
            return _json.loads(PROBE_CACHE.read_text(encoding="utf-8")).get("results", [])
        except Exception:
            pass  # fall through to live run

    import tools.probe_runner as _probe_runner  # local import avoids startup overhead
    results = _probe_runner.run_probes(service_id)

    # Update cache on live run (only when probing all services)
    if not service_id:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            PROBE_CACHE.write_text(
                _json.dumps({
                    "generated_at": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "total": len(results),
                    "drifted": sum(1 for r in results if r["drift"]),
                    "results": results,
                }, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # cache write failure is non-fatal

    return results


if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    expected_api_key = _api_key()

    logging.basicConfig(
        level=os.environ.get("ATLAS_MCP_LOG_LEVEL", "INFO"),
        format="[%(levelname)1.1s %(asctime)s.%(msecs)03d %(name)s] %(message)s",
    )

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)
            if request.url.path.startswith("/.well-known/"):
                return await call_next(request)
            client_host = request.client.host if request.client else ""
            if client_host in ("127.0.0.1", "::1"):
                return await call_next(request)
            if not expected_api_key:
                return await call_next(request)  # no key configured — open
            provided = request.headers.get("x-api-key", "")
            if provided != expected_api_key:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    class LifecycleLoggingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith("/mcp"):
                return await call_next(request)

            response = await call_next(request)

            correlation_id = request.headers.get("x-correlation-id", "")
            request_session_id = request.headers.get("mcp-session-id", "")
            response_session_id = response.headers.get("mcp-session-id", "")
            session_id = request_session_id or response_session_id

            event_type = ""
            tool_name = ""

            if request.method == "DELETE":
                event_type = "session_terminate"
            elif request.method == "POST":
                if response_session_id and not request_session_id:
                    event_type = "session_create"
                else:
                    event_type = "session_use"
                    tool_name = ""

            if event_type:
                _log_session_lifecycle(
                    event_type,
                    correlation_id=correlation_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    status_code=response.status_code,
                )

            return response

    async def services_rest(request: Request) -> JSONResponse:
        """GET /services — returns all service entities as a JSON object keyed by id.

        Intended for machine consumers (e.g. catalog.py) that need structured
        data without navigating the MCP streamable-http protocol.
        """
        import json as _json
        from datetime import datetime as _dt
        store = _get_store()
        services = store.get("service", {})
        result: dict[str, Any] = {}
        for sid, service in sorted(services.items()):
            s = _model_to_dict(service)
            result[sid] = s

        def _default(obj: Any) -> Any:
            if isinstance(obj, _dt):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        return Response(
            content=_json.dumps(result, default=_default),
            media_type="application/json",
        )

    host = os.environ.get("ATLAS_MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ATLAS_MCP_PORT", str(DEFAULT_PORT)))

    async def oauth_resource_metadata(request: Request) -> JSONResponse:
        base = str(request.base_url).rstrip("/")
        return JSONResponse({"resource": base, "authorization_servers": []})

    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/services", services_rest, methods=["GET"]))

    if _OAUTH_ENABLED:
        # P2 — single-operator login at the AS authorize step.
        import html as _html

        from starlette.responses import HTMLResponse, RedirectResponse
        from atlas_oauth import LOGIN_PATH as _OAUTH_LOGIN_PATH

        def _login_page(ticket: str, error: str = "", code: int = 200) -> HTMLResponse:
            t = _html.escape(ticket)
            e = f'<p style="color:#b00">{_html.escape(error)}</p>' if error else ""
            body = (
                "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Atlas sign-in</title>"
                "<div style='max-width:340px;margin:12vh auto;font-family:system-ui'>"
                "<h2>Atlas MCP sign-in</h2>" + e +
                f"<form method=post action='{_OAUTH_LOGIN_PATH}'>"
                f"<input type=hidden name=ticket value='{t}'>"
                "<input type=password name=secret placeholder='Login secret' autofocus "
                "style='width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box'>"
                "<button style='margin-top:.6rem;padding:.6rem 1rem;font-size:1rem'>Sign in</button>"
                "</form></div>"
            )
            return HTMLResponse(body, status_code=code)

        async def oauth_login(request: Request):
            from atlas_oauth import verify_login as _verify_login
            if request.method == "GET":
                return _login_page(request.query_params.get("ticket", ""))
            form = await request.form()
            ticket = str(form.get("ticket", ""))
            if not _verify_login(str(form.get("secret", ""))):
                return _login_page(ticket, "Invalid login secret.", code=401)
            redir = _atlas_oauth_provider.complete_authorization(ticket)
            if not redir:
                return _login_page("", "Login request expired — restart from your client.", code=400)
            return RedirectResponse(redir, status_code=302)

        app.routes.insert(0, Route(_OAUTH_LOGIN_PATH, oauth_login, methods=["GET", "POST"]))

        # P3 — keep 127.0.0.1 callers (local automations) working by injecting a
        # stable local-agent bearer before the SDK's auth runs.
        _local_token = _atlas_oauth_provider.ensure_local_token()

        class _LocalBearerInjector:
            def __init__(self, asgi_app):
                self.app = asgi_app

            async def __call__(self, scope, receive, send):
                if scope.get("type") == "http":
                    client = scope.get("client") or ("", 0)
                    if client[0] in ("127.0.0.1", "::1") and not any(
                        k == b"authorization" for k, _ in scope.get("headers", [])
                    ):
                        scope = dict(scope)
                        scope["headers"] = list(scope["headers"]) + [
                            (b"authorization", b"Bearer " + _local_token.encode())
                        ]
                await self.app(scope, receive, send)
    else:
        # Legacy posture: custom (empty) protected-resource doc + X-API-Key gate.
        app.routes.insert(0, Route("/.well-known/oauth-protected-resource", oauth_resource_metadata, methods=["GET"]))

    app.add_middleware(LifecycleLoggingMiddleware)
    if not _OAUTH_ENABLED:
        app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "accept",
            "authorization",
            "content-type",
            "mcp-protocol-version",
            "mcp-session-id",
            "last-event-id",
            "x-api-key",
            "x-correlation-id",
        ],
        expose_headers=["mcp-session-id", "mcp-protocol-version", "x-correlation-id"],
    )
    if _OAUTH_ENABLED:
        app.add_middleware(_LocalBearerInjector)  # outermost: inject before SDK auth

    uvicorn.run(app, host=host, port=port)
