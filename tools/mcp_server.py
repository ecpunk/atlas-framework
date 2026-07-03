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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from tools.atlas_lock import atlas_write_lock
from tools.llm_client import freeform_respond, get_llm_runtime_info, route_telegram_intent
from tools.store import Store, load_store

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8105
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_API_KEY_FILE = REPO_ROOT / "secrets" / "api_key.txt"

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


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if q <= 0:
        return float(min(values))
    if q >= 1:
        return float(max(values))
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


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
                 "GIT_AUTHOR_EMAIL": "atlas@example.local", "GIT_COMMITTER_EMAIL": "atlas@example.local"},
        )
        if commit.returncode != 0:
            return {"error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"}
        return {"ok": True, "sha": commit.stdout.strip().split()[-1] if commit.stdout.strip() else ""}


def _write_and_commit(entity_dir: str, entity_id: str, data: dict[str, Any], message: str) -> dict[str, Any]:
    """Write entity YAML and commit. Validates schema via load_store after write.

    Promotion cleanup: if a staging stub (staging/<entity_id>.yaml) exists for this
    id, it is removed in the same commit. staging/ is a transient operator-review
    queue; once a stub is promoted into entities/ the review copy must not linger
    (a 2026-06-22 audit found dozens of orphaned already-promoted stubs). Mirrors
    remediate.py's unlink pattern; also opportunistically retires any pre-existing
    stale stub whose id is written here.
    """
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
    # Trigger pipeline post-commit hook validation
    try:
        _get_store()
    except Exception as exc:
        return {"error": f"Entity written and committed but schema validation failed: {exc}"}
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


MCP_INSTRUCTIONS = """You are connected to the Atlas canonical entity store for the Hydra homelab.

Atlas is the single source of truth for stack definitions: projects, services, servers, agents, rules, and vocabularies.

Read tools:
- get_project(id): full project entity
- list_projects(category?, status?): filtered project list
- get_service(id): full service entity
- list_services(): all services with key fields
- get_server(id): server entity
- get_vocabulary(id): vocabulary with all values
- get_rule(id) / list_rules(scope?, severity?): rule entities
- get_agent(id) / list_agents(): agent definitions
- stack_summary(): entity counts — use this for orientation

Bridge / output tools:
- get_output(name): read a generated output file from atlas-store/outputs/ (e.g. "Service Catalog.md")
- get_kb_doc(name): read a knowledge base doc from services/docs/kb/ (e.g. "Start Here.md")
- create_kb_doc(path, content, confirm?, commit?): create docs/kb markdown via Atlas (single sanctioned write path)
- update_kb_doc(path, content, confirm?, commit?): update docs/kb markdown via Atlas (single sanctioned write path)
- append_kb_doc(path, content, expected_hash?, confirm?, commit?): append text to a KB doc server-side (no full-file round-trip; truncation-safe)
- replace_kb_section(path, anchor, content, create_missing?, expected_hash?, confirm?, commit?): replace one markdown section's body by heading anchor, leaving the rest intact
- check_drift(service_id?, force?): reality probes — reads cached result by default; force=True runs live

Write tools (propose-confirm pattern — preview first, then confirm=True to apply):
- add_project(id, name, summary, category, status, concept_doc, gdrive_folder?, domain_tags?): add new project (autonomous)
- update_project(id, confirm?, status?, category?, summary?, domain_tags?, ...): update project fields
- add_service(id, name, summary, service_type, lifecycle, deployment_path, ...): add new service (autonomous)
- update_service(id, confirm?, lifecycle?, port?, ...): update service fields
- retire_service(id, confirm?): set service lifecycle to retired
- add_task(project, title, next_action, closure_test, ...): add canonical task (autonomous)
- update_task(id, confirm?, status?, priority?, ...): update task fields

Task query tools:
- get_task(id): read one task
- list_tasks(project?, status?, priority?, limit?): list tasks with optional filters
- list_actionable_tasks(project?, limit?): list only open/in_progress tasks (agent-facing default)
- list_open_tasks(project?, limit?): list only open/in_progress/blocked tasks (operator-facing visibility)

Session query tools:
- get_session(id): read one session
- list_sessions(source?, status?, lifecycle?, user_id?, project_id?, limit?): list sessions with filters
- session_touch_history(project_id?, source?, lifecycle?, since_hours?, limit?): recent sessions that touched projects
- session_activity_summary(source?, since_hours?): aggregate session activity including off-hours ratio
- session_activity_deltas(source?, current_hours?, baseline_hours?): week-over-week style trend deltas and anomaly prompts
- session_operator_heatmap(source?, since_hours?, limit?): per-operator activity slices and service heatmap
- session_baseline_alerts(source?, current_hours?, baseline_hours?, volume_delta_pct_threshold?, off_hours_delta_pp_threshold?, operator_spike_multiplier?): proactive baseline drift alerts
- session_adaptive_thresholds(source?, current_hours?, history_hours?, quantile?, min_windows?): adaptive threshold profiles and alerts

Session write tools:
- add_session(user_id, ...): add canonical session entity (autonomous)
- update_session(id, confirm?, status?, summary?, ...): update existing session entity
- archive_session(id, confirm?, reason?): set lifecycle to archived with timestamp
- prune_session(id, confirm?, reason?): remove transcript while preserving required audit metadata

Use stack_summary() first when you need an overview of what's in the store.
"""

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
    instructions=MCP_INSTRUCTIONS,
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


@mcp.tool()
def route_telegram_intent_tool(user_text: str, context_hint: str = "") -> dict[str, Any]:
    """Route a Telegram ops message into a structured intent using the Atlas LLM policy gate.

    Returns JSON with intent, side_effect, confidence, action/container/service/lines, and model usage metadata.
    """
    if not user_text.strip():
        return {
            "intent": "unknown",
            "side_effect": "none",
            "confidence": 0.0,
            "action": "",
            "container": "",
            "service": "",
            "lines": 50,
            "reason": "empty input",
            "error": True,
            "cost_capped": False,
        }
    return route_telegram_intent(user_text=user_text, context_hint=context_hint)


@mcp.tool()
def freeform_respond_tool(user_text: str, context_hint: str = "") -> dict[str, Any]:
    """Run a freeform agentic response using Sonnet + live MCP tool access.

    Intended for Telegram messages that don't match a structured ops intent.
    The agent has read-only access to: ops_server_status, ops_docker_status,
    ops_service_logs (via hydra_ops_mcp) and atlas_stack_summary, atlas_list_services,
    atlas_get_service, atlas_list_projects, atlas_get_project, atlas_check_drift,
    atlas_session_activity_summary (internal).

    Returns {"response": str, "model": str, "tokens_in": int, "tokens_out": int,
             "tool_calls": int, "error": bool, "cost_capped": bool}.
    """
    if not user_text.strip():
        return {
            "response": "No input provided.",
            "model": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "tool_calls": 0,
            "error": True,
            "cost_capped": False,
        }
    return freeform_respond(user_text=user_text, context_hint=context_hint)


@mcp.tool()
def llm_cost_summary_tool() -> dict[str, Any]:
    """Return Atlas LLM runtime model and spend summary (daily + process run)."""
    return get_llm_runtime_info()


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
def session_touch_history(
    project_id: str = "",
    source: str = "",
    lifecycle: str = "",
    since_hours: int = 168,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recent sessions that touched one or more projects, ordered by timestamp desc."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if since_hours < 1 or since_hours > 24 * 365:
        raise ValueError("since_hours must be between 1 and 8760")
    if lifecycle and lifecycle not in _SESSION_LIFECYCLE_VALUES:
        raise ValueError(f"lifecycle must be one of: {sorted(_SESSION_LIFECYCLE_VALUES)}")

    store = _get_store()
    sessions = store.get("session", {})
    cutoff = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)

    rows: list[tuple[float, dict[str, Any]]] = []
    for session_id, session in sessions.items():
        payload = _model_to_dict(session)
        if source and payload.get("source") != source:
            continue
        if lifecycle and payload.get("lifecycle", "active") != lifecycle:
            continue

        projects = payload.get("project_ids") or []
        if project_id and project_id not in projects:
            continue
        if not projects:
            continue

        ts_dt = _parse_iso_or_none(payload.get("timestamp"))
        ts_epoch = ts_dt.timestamp() if ts_dt else 0.0
        if ts_epoch and ts_epoch < cutoff:
            continue

        rows.append(
            (
                ts_epoch,
                {
                    "id": session_id,
                    "timestamp": payload.get("timestamp"),
                    "source": payload.get("source"),
                    "status": payload.get("status"),
                    "lifecycle": payload.get("lifecycle", "active"),
                    "project_ids": projects,
                    "summary": payload.get("summary"),
                    "tool_call_count": len(payload.get("tool_calls") or []),
                },
            )
        )

    rows.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in rows[:limit]]


@mcp.tool()
def session_activity_summary(source: str = "", since_hours: int = 168) -> dict[str, Any]:
    """Return aggregate session activity and off-hours ratio for operator analytics."""
    if since_hours < 1 or since_hours > 24 * 365:
        raise ValueError("since_hours must be between 1 and 8760")

    store = _get_store()
    sessions = store.get("session", {})
    cutoff = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)

    total = 0
    off_hours = 0
    by_lifecycle: dict[str, int] = {"active": 0, "archived": 0, "pruned": 0}
    by_project: dict[str, int] = {}

    for session in sessions.values():
        payload = _model_to_dict(session)
        if source and payload.get("source") != source:
            continue
        ts_dt = _parse_iso_or_none(payload.get("timestamp"))
        ts_epoch = ts_dt.timestamp() if ts_dt else 0.0
        if ts_epoch and ts_epoch < cutoff:
            continue

        total += 1

        lifecycle = str(payload.get("lifecycle", "active"))
        by_lifecycle[lifecycle] = by_lifecycle.get(lifecycle, 0) + 1

        if ts_dt is not None:
            # UTC off-hours heuristic: before 07:00 or after/equal 18:00.
            if ts_dt.hour < 7 or ts_dt.hour >= 18:
                off_hours += 1

        for project in payload.get("project_ids") or []:
            by_project[project] = by_project.get(project, 0) + 1

    top_projects = sorted(by_project.items(), key=lambda item: item[1], reverse=True)[:10]

    return {
        "window_hours": since_hours,
        "source": source or "*",
        "total_sessions": total,
        "off_hours_sessions": off_hours,
        "off_hours_ratio": round((off_hours / total), 4) if total else 0.0,
        "by_lifecycle": by_lifecycle,
        "top_projects": [{"project_id": pid, "count": count} for pid, count in top_projects],
    }


@mcp.tool()
def session_activity_deltas(
    source: str = "",
    current_hours: int = 168,
    baseline_hours: int = 168,
) -> dict[str, Any]:
    """Return window-over-window session deltas and anomaly triage prompt suggestions."""
    if current_hours < 1 or current_hours > 24 * 365:
        raise ValueError("current_hours must be between 1 and 8760")
    if baseline_hours < 1 or baseline_hours > 24 * 365:
        raise ValueError("baseline_hours must be between 1 and 8760")

    def _window_summary(
        sessions: dict[str, Any],
        source_filter: str,
        start_epoch: float,
        end_epoch: float,
    ) -> dict[str, Any]:
        total = 0
        off_hours = 0
        by_lifecycle: dict[str, int] = {"active": 0, "archived": 0, "pruned": 0}
        by_project: dict[str, int] = {}

        for session in sessions.values():
            payload = _model_to_dict(session)
            if source_filter and payload.get("source") != source_filter:
                continue
            ts_dt = _parse_iso_or_none(payload.get("timestamp"))
            if ts_dt is None:
                continue
            ts_epoch = ts_dt.timestamp()
            if ts_epoch < start_epoch or ts_epoch >= end_epoch:
                continue

            total += 1
            lifecycle = str(payload.get("lifecycle", "active"))
            by_lifecycle[lifecycle] = by_lifecycle.get(lifecycle, 0) + 1
            if ts_dt.hour < 7 or ts_dt.hour >= 18:
                off_hours += 1
            for project in payload.get("project_ids") or []:
                by_project[project] = by_project.get(project, 0) + 1

        return {
            "total": total,
            "off_hours": off_hours,
            "off_hours_ratio": (off_hours / total) if total else 0.0,
            "by_lifecycle": by_lifecycle,
            "by_project": by_project,
        }

    now_epoch = datetime.now(timezone.utc).timestamp()
    current_start = now_epoch - (current_hours * 3600)
    baseline_end = current_start
    baseline_start = baseline_end - (baseline_hours * 3600)

    store = _get_store()
    sessions = store.get("session", {})

    current = _window_summary(sessions, source, current_start, now_epoch)
    baseline = _window_summary(sessions, source, baseline_start, baseline_end)

    total_delta = current["total"] - baseline["total"]
    if baseline["total"]:
        total_delta_pct = round((total_delta / baseline["total"]), 4)
    else:
        total_delta_pct = None

    off_hours_ratio_delta = round(current["off_hours_ratio"] - baseline["off_hours_ratio"], 4)

    all_projects = set(current["by_project"].keys()) | set(baseline["by_project"].keys())
    project_deltas: list[dict[str, Any]] = []
    for project_id in all_projects:
        c = int(current["by_project"].get(project_id, 0))
        b = int(baseline["by_project"].get(project_id, 0))
        delta = c - b
        if delta == 0:
            continue
        project_deltas.append(
            {
                "project_id": project_id,
                "current": c,
                "baseline": b,
                "delta": delta,
            }
        )
    project_deltas.sort(key=lambda item: abs(int(item["delta"])), reverse=True)

    prompts: list[str] = []
    if total_delta >= 3:
        prompts.append(
            "Session volume is elevated vs baseline. Which projects or tools account for the increase?"
        )
    if total_delta <= -3:
        prompts.append(
            "Session volume dropped vs baseline. Were automation paths changed or is operator activity missing?"
        )
    if off_hours_ratio_delta >= 0.2:
        prompts.append(
            "Off-hours activity ratio increased materially. Were these incident-driven sessions or expected maintenance windows?"
        )
    if off_hours_ratio_delta <= -0.2:
        prompts.append(
            "Off-hours activity ratio decreased materially. Confirm whether alert pressure has normalized."
        )
    if int(current["by_lifecycle"].get("active", 0)) >= 10 and int(current["by_lifecycle"].get("pruned", 0)) == 0:
        prompts.append(
            "Active sessions accumulated without pruning. Should retention sweep/archive-prune workflow run now?"
        )

    return {
        "source": source or "*",
        "window": {
            "current_hours": current_hours,
            "baseline_hours": baseline_hours,
        },
        "current": {
            "total_sessions": current["total"],
            "off_hours_sessions": current["off_hours"],
            "off_hours_ratio": round(current["off_hours_ratio"], 4),
            "by_lifecycle": current["by_lifecycle"],
        },
        "baseline": {
            "total_sessions": baseline["total"],
            "off_hours_sessions": baseline["off_hours"],
            "off_hours_ratio": round(baseline["off_hours_ratio"], 4),
            "by_lifecycle": baseline["by_lifecycle"],
        },
        "deltas": {
            "total_sessions": total_delta,
            "total_sessions_pct": total_delta_pct,
            "off_hours_ratio": off_hours_ratio_delta,
            "project_deltas": project_deltas[:10],
        },
        "anomaly_prompts": prompts,
    }


@mcp.tool()
def session_operator_heatmap(source: str = "", since_hours: int = 168, limit: int = 20) -> dict[str, Any]:
    """Return per-operator activity slices and service-level touch heatmap from sessions."""
    if since_hours < 1 or since_hours > 24 * 365:
        raise ValueError("since_hours must be between 1 and 8760")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")

    def _normalize_service_token(raw: Any) -> str:
        token = str(raw or "").strip().lower()
        if not token:
            return ""
        if ":" in token:
            prefix, rest = token.split(":", 1)
            if prefix in {"service", "container"} and rest.strip():
                token = rest.strip()
        return token

    store = _get_store()
    sessions = store.get("session", {})
    cutoff = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)

    operator_summary: dict[str, dict[str, Any]] = {}
    operator_service_counts: dict[str, dict[str, int]] = {}
    service_counts: dict[str, int] = {}
    total_sessions = 0

    for session in sessions.values():
        payload = _model_to_dict(session)
        if source and payload.get("source") != source:
            continue

        ts_dt = _parse_iso_or_none(payload.get("timestamp"))
        ts_epoch = ts_dt.timestamp() if ts_dt else 0.0
        if ts_epoch and ts_epoch < cutoff:
            continue

        total_sessions += 1
        operator_id = str(payload.get("user_id", "unknown"))
        lifecycle = str(payload.get("lifecycle", "active"))

        if operator_id not in operator_summary:
            operator_summary[operator_id] = {
                "user_id": int(payload.get("user_id", 0) or 0),
                "total_sessions": 0,
                "off_hours_sessions": 0,
                "by_lifecycle": {"active": 0, "archived": 0, "pruned": 0},
            }
            operator_service_counts[operator_id] = {}

        operator_summary[operator_id]["total_sessions"] += 1
        operator_summary[operator_id]["by_lifecycle"][lifecycle] = (
            operator_summary[operator_id]["by_lifecycle"].get(lifecycle, 0) + 1
        )
        if ts_dt is not None and (ts_dt.hour < 7 or ts_dt.hour >= 18):
            operator_summary[operator_id]["off_hours_sessions"] += 1

        session_services: set[str] = set()
        for entity in payload.get("entities_touched") or []:
            if str(entity).strip().lower().startswith(("service:", "container:")):
                normalized = _normalize_service_token(entity)
                if normalized:
                    session_services.add(normalized)

        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = call.get("args") or {}
            if isinstance(args, dict):
                for key in ("service", "container", "name"):
                    if key in args:
                        normalized = _normalize_service_token(args.get(key))
                        if normalized:
                            session_services.add(normalized)

        for service_name in session_services:
            service_counts[service_name] = service_counts.get(service_name, 0) + 1
            user_service = operator_service_counts[operator_id]
            user_service[service_name] = user_service.get(service_name, 0) + 1

    operator_rows: list[dict[str, Any]] = []
    for operator_id, summary in operator_summary.items():
        total = int(summary.get("total_sessions", 0))
        off_hours = int(summary.get("off_hours_sessions", 0))
        svc = operator_service_counts.get(operator_id, {})
        top_services = sorted(svc.items(), key=lambda item: item[1], reverse=True)[:5]
        operator_rows.append(
            {
                "user_id": int(summary.get("user_id", 0)),
                "total_sessions": total,
                "off_hours_sessions": off_hours,
                "off_hours_ratio": round((off_hours / total), 4) if total else 0.0,
                "by_lifecycle": summary.get("by_lifecycle", {}),
                "top_services": [{"service": name, "count": count} for name, count in top_services],
            }
        )

    operator_rows.sort(key=lambda item: int(item.get("total_sessions", 0)), reverse=True)
    top_services_overall = sorted(service_counts.items(), key=lambda item: item[1], reverse=True)[:limit]

    return {
        "source": source or "*",
        "window_hours": since_hours,
        "total_sessions": total_sessions,
        "operator_count": len(operator_rows),
        "operators": operator_rows[:limit],
        "service_heatmap": [{"service": name, "count": count} for name, count in top_services_overall],
    }


@mcp.tool()
def session_baseline_alerts(
    source: str = "",
    current_hours: int = 168,
    baseline_hours: int = 168,
    volume_delta_pct_threshold: float = 0.5,
    off_hours_delta_pp_threshold: float = 20.0,
    operator_spike_multiplier: float = 2.0,
    min_operator_sessions: int = 3,
) -> dict[str, Any]:
    """Return proactive baseline drift alerts for session volume, off-hours ratio, and operator intensity."""
    if current_hours < 1 or current_hours > 24 * 365:
        raise ValueError("current_hours must be between 1 and 8760")
    if baseline_hours < 1 or baseline_hours > 24 * 365:
        raise ValueError("baseline_hours must be between 1 and 8760")
    if volume_delta_pct_threshold <= 0:
        raise ValueError("volume_delta_pct_threshold must be > 0")
    if off_hours_delta_pp_threshold <= 0:
        raise ValueError("off_hours_delta_pp_threshold must be > 0")
    if operator_spike_multiplier <= 1:
        raise ValueError("operator_spike_multiplier must be > 1")
    if min_operator_sessions < 1:
        raise ValueError("min_operator_sessions must be >= 1")

    def _window_summary(
        sessions: dict[str, Any],
        source_filter: str,
        start_epoch: float,
        end_epoch: float,
    ) -> dict[str, Any]:
        total = 0
        off_hours = 0
        by_operator: dict[int, int] = {}
        by_lifecycle: dict[str, int] = {"active": 0, "archived": 0, "pruned": 0}

        for session in sessions.values():
            payload = _model_to_dict(session)
            if source_filter and payload.get("source") != source_filter:
                continue

            ts_dt = _parse_iso_or_none(payload.get("timestamp"))
            if ts_dt is None:
                continue
            ts_epoch = ts_dt.timestamp()
            if ts_epoch < start_epoch or ts_epoch >= end_epoch:
                continue

            total += 1
            lifecycle = str(payload.get("lifecycle", "active"))
            by_lifecycle[lifecycle] = by_lifecycle.get(lifecycle, 0) + 1

            user_id = int(payload.get("user_id", 0) or 0)
            by_operator[user_id] = by_operator.get(user_id, 0) + 1

            if ts_dt.hour < 7 or ts_dt.hour >= 18:
                off_hours += 1

        return {
            "total": total,
            "off_hours": off_hours,
            "off_hours_ratio": (off_hours / total) if total else 0.0,
            "by_operator": by_operator,
            "by_lifecycle": by_lifecycle,
        }

    now_epoch = datetime.now(timezone.utc).timestamp()
    current_start = now_epoch - (current_hours * 3600)
    baseline_end = current_start
    baseline_start = baseline_end - (baseline_hours * 3600)

    store = _get_store()
    sessions = store.get("session", {})

    current = _window_summary(sessions, source, current_start, now_epoch)
    baseline = _window_summary(sessions, source, baseline_start, baseline_end)

    alerts: list[dict[str, Any]] = []
    recommendations: list[str] = []

    baseline_total = int(baseline["total"])
    current_total = int(current["total"])
    total_delta = current_total - baseline_total
    total_delta_pct = (total_delta / baseline_total) if baseline_total else None

    if total_delta_pct is not None and abs(total_delta_pct) >= volume_delta_pct_threshold:
        direction = "increase" if total_delta_pct > 0 else "decrease"
        alerts.append(
            {
                "severity": "high" if abs(total_delta_pct) >= (2 * volume_delta_pct_threshold) else "medium",
                "signal": "session_volume_drift",
                "direction": direction,
                "delta": total_delta,
                "delta_pct": round(total_delta_pct, 4),
                "threshold": volume_delta_pct_threshold,
            }
        )
        recommendations.append(
            "Review top project and service movers, then classify whether volume drift is incident-driven or expected workload change."
        )

    off_ratio_current = float(current["off_hours_ratio"])
    off_ratio_baseline = float(baseline["off_hours_ratio"])
    off_delta_pp = (off_ratio_current - off_ratio_baseline) * 100.0
    if abs(off_delta_pp) >= off_hours_delta_pp_threshold:
        direction = "increase" if off_delta_pp > 0 else "decrease"
        alerts.append(
            {
                "severity": "high" if abs(off_delta_pp) >= (2 * off_hours_delta_pp_threshold) else "medium",
                "signal": "off_hours_ratio_drift",
                "direction": direction,
                "delta_pp": round(off_delta_pp, 2),
                "threshold_pp": off_hours_delta_pp_threshold,
            }
        )
        recommendations.append(
            "Validate off-hours spikes against maintenance windows and alert bursts; escalate if unexplained."
        )

    operator_spikes: list[dict[str, Any]] = []
    all_operators = set(current["by_operator"].keys()) | set(baseline["by_operator"].keys())
    for user_id in all_operators:
        cur = int(current["by_operator"].get(user_id, 0))
        base = int(baseline["by_operator"].get(user_id, 0))
        if cur < min_operator_sessions:
            continue
        if base == 0:
            if cur >= int(min_operator_sessions * operator_spike_multiplier):
                operator_spikes.append({"user_id": user_id, "current": cur, "baseline": base, "ratio": None})
            continue
        ratio = cur / base
        if ratio >= operator_spike_multiplier:
            operator_spikes.append({"user_id": user_id, "current": cur, "baseline": base, "ratio": round(ratio, 2)})

    if operator_spikes:
        alerts.append(
            {
                "severity": "medium",
                "signal": "operator_intensity_spike",
                "spikes": operator_spikes[:10],
                "threshold_multiplier": operator_spike_multiplier,
            }
        )
        recommendations.append(
            "Review operator-level concentration for potential paging imbalance, runbook friction, or single-operator overload."
        )

    if not alerts:
        recommendations.append("No baseline drift alert crossed configured thresholds in this comparison window.")

    return {
        "source": source or "*",
        "window": {"current_hours": current_hours, "baseline_hours": baseline_hours},
        "thresholds": {
            "volume_delta_pct_threshold": volume_delta_pct_threshold,
            "off_hours_delta_pp_threshold": off_hours_delta_pp_threshold,
            "operator_spike_multiplier": operator_spike_multiplier,
            "min_operator_sessions": min_operator_sessions,
        },
        "current": {
            "total_sessions": current_total,
            "off_hours_ratio": round(off_ratio_current, 4),
            "by_lifecycle": current["by_lifecycle"],
        },
        "baseline": {
            "total_sessions": baseline_total,
            "off_hours_ratio": round(off_ratio_baseline, 4),
            "by_lifecycle": baseline["by_lifecycle"],
        },
        "alerts": alerts,
        "recommendations": recommendations,
    }


@mcp.tool()
def session_adaptive_thresholds(
    source: str = "",
    current_hours: int = 168,
    history_hours: int = 24 * 56,
    quantile: float = 0.75,
    min_windows: int = 4,
    sensitivity_floor: float = 0.8,
    sensitivity_ceiling: float = 1.8,
) -> dict[str, Any]:
    """Return adaptive threshold profiles using rolling quantiles and per-project sensitivity."""
    if current_hours < 1 or current_hours > 24 * 365:
        raise ValueError("current_hours must be between 1 and 8760")
    if history_hours < current_hours * 2:
        raise ValueError("history_hours must be at least 2 * current_hours")
    if quantile <= 0 or quantile >= 1:
        raise ValueError("quantile must be between 0 and 1 (exclusive)")
    if min_windows < 2:
        raise ValueError("min_windows must be at least 2")
    if sensitivity_floor <= 0:
        raise ValueError("sensitivity_floor must be > 0")
    if sensitivity_ceiling < sensitivity_floor:
        raise ValueError("sensitivity_ceiling must be >= sensitivity_floor")

    def _window_counts(
        sessions: dict[str, Any],
        source_filter: str,
        start_epoch: float,
        end_epoch: float,
    ) -> tuple[int, dict[str, int]]:
        total = 0
        by_project: dict[str, int] = {}
        for session in sessions.values():
            payload = _model_to_dict(session)
            if source_filter and payload.get("source") != source_filter:
                continue
            ts_dt = _parse_iso_or_none(payload.get("timestamp"))
            if ts_dt is None:
                continue
            ts_epoch = ts_dt.timestamp()
            if ts_epoch < start_epoch or ts_epoch >= end_epoch:
                continue
            total += 1
            for project_id in payload.get("project_ids") or []:
                by_project[project_id] = by_project.get(project_id, 0) + 1
        return total, by_project

    store = _get_store()
    sessions = store.get("session", {})
    now_epoch = datetime.now(timezone.utc).timestamp()

    current_start = now_epoch - (current_hours * 3600)
    current_total, current_projects = _window_counts(sessions, source, current_start, now_epoch)

    window_count = max(1, history_hours // current_hours)
    historical_totals: list[float] = []
    project_history: dict[str, list[float]] = {}

    for i in range(1, window_count + 1):
        end_epoch = now_epoch - (i * current_hours * 3600)
        start_epoch = end_epoch - (current_hours * 3600)
        total, projects = _window_counts(sessions, source, start_epoch, end_epoch)
        historical_totals.append(float(total))
        for project_id, count in projects.items():
            project_history.setdefault(project_id, []).append(float(count))

    profiles: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []

    project_ids = set(project_history.keys()) | set(current_projects.keys())
    for project_id in sorted(project_ids):
        history = project_history.get(project_id, [])
        current_count = int(current_projects.get(project_id, 0))
        if len(history) < min_windows:
            profiles.append(
                {
                    "project_id": project_id,
                    "current": current_count,
                    "history_windows": len(history),
                    "adaptive_threshold": None,
                    "sensitivity": None,
                    "note": "insufficient_history",
                }
            )
            continue

        p50 = _quantile(history, 0.5)
        p90 = _quantile(history, 0.9)
        base = _quantile(history, quantile)
        variability = (p90 / max(p50, 1.0)) if p90 > 0 else 1.0
        sensitivity = _clamp(1.0 + ((variability - 1.0) * 0.5), sensitivity_floor, sensitivity_ceiling)
        threshold = base * sensitivity

        profiles.append(
            {
                "project_id": project_id,
                "current": current_count,
                "history_windows": len(history),
                "p50": round(p50, 3),
                "p90": round(p90, 3),
                "base_quantile": round(base, 3),
                "variability": round(variability, 3),
                "sensitivity": round(sensitivity, 3),
                "adaptive_threshold": round(threshold, 3),
            }
        )

        if float(current_count) > threshold:
            alerts.append(
                {
                    "signal": "project_session_intensity_above_adaptive_threshold",
                    "project_id": project_id,
                    "current": current_count,
                    "threshold": round(threshold, 3),
                    "excess": round(float(current_count) - threshold, 3),
                    "severity": "high" if float(current_count) > (threshold * 1.5) else "medium",
                }
            )

    global_profile: dict[str, Any]
    if len(historical_totals) < min_windows:
        global_profile = {
            "current": current_total,
            "history_windows": len(historical_totals),
            "adaptive_threshold": None,
            "note": "insufficient_history",
        }
    else:
        g_p50 = _quantile(historical_totals, 0.5)
        g_p90 = _quantile(historical_totals, 0.9)
        g_base = _quantile(historical_totals, quantile)
        g_variability = (g_p90 / max(g_p50, 1.0)) if g_p90 > 0 else 1.0
        g_sensitivity = _clamp(1.0 + ((g_variability - 1.0) * 0.5), sensitivity_floor, sensitivity_ceiling)
        g_threshold = g_base * g_sensitivity
        global_profile = {
            "current": current_total,
            "history_windows": len(historical_totals),
            "p50": round(g_p50, 3),
            "p90": round(g_p90, 3),
            "base_quantile": round(g_base, 3),
            "variability": round(g_variability, 3),
            "sensitivity": round(g_sensitivity, 3),
            "adaptive_threshold": round(g_threshold, 3),
        }
        if float(current_total) > g_threshold:
            alerts.insert(
                0,
                {
                    "signal": "global_session_volume_above_adaptive_threshold",
                    "current": current_total,
                    "threshold": round(g_threshold, 3),
                    "excess": round(float(current_total) - g_threshold, 3),
                    "severity": "high" if float(current_total) > (g_threshold * 1.5) else "medium",
                },
            )

    recommendations: list[str] = []
    if alerts:
        recommendations.append("Inspect alerted projects against top service and operator heatmaps before opening follow-on tasks.")
        recommendations.append("For high-variance projects, tune sensitivity profile rather than suppressing alerts globally.")
    else:
        recommendations.append("No adaptive threshold breaches detected for this comparison horizon.")

    return {
        "source": source or "*",
        "window": {
            "current_hours": current_hours,
            "history_hours": history_hours,
            "history_windows": window_count,
        },
        "model": {
            "quantile": quantile,
            "min_windows": min_windows,
            "sensitivity_floor": sensitivity_floor,
            "sensitivity_ceiling": sensitivity_ceiling,
        },
        "global_profile": global_profile,
        "project_profiles": profiles[:50],
        "alerts": alerts,
        "recommendations": recommendations,
    }


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
def get_vocabulary(id: str) -> dict[str, Any]:
    """Return a vocabulary with all its values. Example ids: lifecycle_categories, service_types, agent_lanes."""
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
            "scope": r.get("scope", {}).get("value_id"),
            "severity": r.get("severity", {}).get("value_id"),
            "applies_to": r.get("applies_to"),
            "must_satisfy": r.get("must_satisfy", "").strip(),
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


@mcp.tool()
def list_agents() -> list[dict[str, Any]]:
    """List all agent entities with key capability fields."""
    store = _get_store()
    agents = store.get("agent", {})
    result = []
    for aid, agent in sorted(agents.items()):
        a = _model_to_dict(agent)
        result.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "model_family": a.get("model_family"),
            "interface": a.get("interface", {}).get("value_id"),
            "primary_lane": a.get("primary_lane", {}).get("value_id"),
        })
    return result


@mcp.tool()
def get_agent(id: str) -> dict[str, Any]:
    """Return the full agent entity for the given id."""
    store = _get_store()
    agents = store.get("agent", {})
    agent = agents.get(id)
    if agent is None:
        ids = sorted(agents.keys())
        raise ValueError(f"Agent '{id}' not found. Known ids: {ids}")
    return _model_to_dict(agent)


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

    category: vocab value_id, e.g. 'current', 'live', 'blocked', 'defer', 'archive'
    status: vocab value_id, e.g. 'concept', 'active', 'paused', 'complete'
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
    host: str = "server:hydra",
    owned_by: str = "",
    health_endpoint: str = "",
    systemd_unit: str = "",
    container_name: str = "",
    restartable: str = "",
    tier: str = "",
    expected_state: str = "",
    protected_high_use: str = "",
    remote: str = "",
) -> dict[str, Any]:
    """Add a new service entity to the Atlas store. Autonomous — writes and commits immediately.

    service_type: vocab value_id, e.g. 'mcp_http', 'docker_compose', 'systemd', 'static'
    lifecycle: vocab value_id, e.g. 'running', 'stopped', 'retired', 'planned'
    host: TypedRef string, default 'server:hydra'
    owned_by: TypedRef string, e.g. 'project:atlas' (optional)
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
    restartable: str = "",
    tier: str = "",
    expected_state: str = "",
    protected_high_use: str = "",
    source_of_truth_doc: str = "",
    summary: str = "",
    remote: str = "",
) -> dict[str, Any]:
    """Update fields on an existing service entity.

    Returns a before/after preview when confirm=False (default). Call again with confirm=True to apply.
    lifecycle accepts value_ids (e.g. 'running', 'stopped', 'retired').
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
def retire_service(id: str, confirm: bool = False) -> dict[str, Any]:
    """Set a service lifecycle to 'retired' in the Atlas store.

    Returns a preview when confirm=False (default). Call again with confirm=True to apply.
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
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "current_lifecycle": current_data.get("lifecycle"),
            "new_lifecycle": "vocab:service_lifecycles:retired",
            "note": "Call retire_service(id=..., confirm=True) to apply.",
        }

    return _write_and_commit("services", id, after, f"chore: retire service {id} via Atlas Write API")


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
# Consolidated memories + trails (the consolidation loop, "the consolidation loop").
# See the consolidation design notes.
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
def add_memory(
    memory_type: str,
    statement: str,
    confidence: float = 0.0,
    status: str = "active",
    superseded_by: str = "",
    provenance: list[str] | None = None,
    recurrence_sessions: int = 0,
    first_seen: str = "",
    last_seen: str = "",
) -> dict[str, Any]:
    """Add a consolidated memory. Id is derived from the statement (stable, dedups re-proposals).

    memory_type: identity | preference | expertise | decision | reference
    statement: one durable, self-contained sentence
    provenance: shared-key Layer-3 pointers back to Substrate traces (episode_id / session:turn / path#section)
    To supersede an existing memory, set status='superseded' and superseded_by=<new memory id>.
    """
    if memory_type not in _MEMORY_TYPE_VALUES:
        raise ValueError(f"memory_type must be one of: {sorted(_MEMORY_TYPE_VALUES)}")
    if status not in _MEMORY_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_MEMORY_STATUS_VALUES)}")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("confidence must be between 0.0 and 1.0")
    if status == "superseded" and not superseded_by:
        raise ValueError("superseded memories must set superseded_by")

    now = _now_iso()
    digest = hashlib.sha256(statement.strip().lower().encode("utf-8")).hexdigest()[:8]
    memory_id = f"memory-{memory_type}-{digest}"

    data: dict[str, Any] = {
        "id": memory_id,
        "memory_type": f"vocab:memory_types:{memory_type}",
        "statement": statement,
        "confidence": confidence,
        "status": status,
        "provenance": provenance or [],
        "recurrence_sessions": recurrence_sessions,
        "first_seen": first_seen or now,
        "last_seen": last_seen or now,
        "created_at": now,
        "updated_at": now,
    }
    if status == "superseded":
        data["superseded_by"] = superseded_by

    result = _write_and_commit("memory", memory_id, data, f"feat: add memory {memory_id} via Atlas Write API")
    if "error" in result:
        return result
    return {"ok": True, "memory_id": memory_id, **result}


@mcp.tool()
def update_memory(
    id: str,
    confirm: bool = False,
    statement: str = "",
    confidence: float = -1.0,
    status: str = "",
    superseded_by: str = "",
    recurrence_sessions: int = -1,
    add_provenance: list[str] | None = None,
) -> dict[str, Any]:
    """Update a memory via propose-confirm. To supersede it, set status='superseded'
    and superseded_by=<new memory id>."""
    store = _get_store()
    memories = store.get("memory", {})
    if id not in memories:
        raise ValueError(f"Memory '{id}' not found. Known ids: {sorted(memories.keys())}")

    rel_path = f"entities/memory/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if statement:
        after["statement"] = statement
    if confidence >= 0.0:
        if confidence > 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        after["confidence"] = confidence
    if status:
        if status not in _MEMORY_STATUS_VALUES:
            raise ValueError(f"status must be one of: {sorted(_MEMORY_STATUS_VALUES)}")
        after["status"] = status
    if superseded_by:
        after["superseded_by"] = superseded_by
    if recurrence_sessions >= 0:
        after["recurrence_sessions"] = recurrence_sessions
    if add_provenance:
        after["provenance"] = list(current_data.get("provenance", []) or []) + list(add_provenance)

    effective_status = after.get("status", "active")
    if effective_status != "superseded":
        after.pop("superseded_by", None)
    elif not after.get("superseded_by"):
        raise ValueError("superseding a memory requires superseded_by")

    after["last_seen"] = _now_iso()
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_memory(id=..., confirm=True, ...) with the same args to apply.",
        }
    return _write_and_commit("memory", id, after, f"chore: update memory {id} via Atlas Write API")


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
def add_trail(
    title: str,
    signal: str,
    question: str,
    connects: list[str] | None = None,
    status: str = "open",
    score: float = 0.0,
    provenance: list[str] | None = None,
) -> dict[str, Any]:
    """Add a trail (exploratory lead — a noticed adjacency worth pulling).

    status: open | pulled | led-somewhere | dead   (new trails are normally 'open')
    connects: the 2+ ideas/clusters it bridges
    signal: why they seem adjacent; question: the open thread to pull
    provenance: shared-key Layer-3 pointers back to Substrate traces
    """
    if status not in _TRAIL_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_TRAIL_STATUS_VALUES)}")
    if not (0.0 <= score <= 1.0):
        raise ValueError("score must be between 0.0 and 1.0")

    now = _now_iso()
    digest = hashlib.sha256(title.strip().lower().encode("utf-8")).hexdigest()[:8]
    trail_id = f"trail-{_slugify_token(title)[:40]}-{digest}"

    data: dict[str, Any] = {
        "id": trail_id,
        "title": title,
        "connects": connects or [],
        "signal": signal,
        "question": question,
        "status": f"vocab:trail_statuses:{status}",
        "score": score,
        "provenance": provenance or [],
        "first_seen": now,
        "last_seen": now,
        "created_at": now,
        "updated_at": now,
    }
    result = _write_and_commit("trail", trail_id, data, f"feat: add trail {trail_id} via Atlas Write API")
    if "error" in result:
        return result
    return {"ok": True, "trail_id": trail_id, **result}


@mcp.tool()
def update_trail(
    id: str,
    confirm: bool = False,
    status: str = "",
    score: float = -1.0,
    signal: str = "",
    question: str = "",
    connects: list[str] | None = None,
    add_provenance: list[str] | None = None,
) -> dict[str, Any]:
    """Update a trail via propose-confirm. Common use: advance status
    (open -> pulled -> led-somewhere|dead)."""
    store = _get_store()
    trails = store.get("trail", {})
    if id not in trails:
        raise ValueError(f"Trail '{id}' not found. Known ids: {sorted(trails.keys())}")

    rel_path = f"entities/trail/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if status:
        if status not in _TRAIL_STATUS_VALUES:
            raise ValueError(f"status must be one of: {sorted(_TRAIL_STATUS_VALUES)}")
        after["status"] = f"vocab:trail_statuses:{status}"
    if score >= 0.0:
        if score > 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        after["score"] = score
    if signal:
        after["signal"] = signal
    if question:
        after["question"] = question
    if connects is not None:
        after["connects"] = connects
    if add_provenance:
        after["provenance"] = list(current_data.get("provenance", []) or []) + list(add_provenance)

    after["last_seen"] = _now_iso()
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_trail(id=..., confirm=True, ...) with the same args to apply.",
        }
    return _write_and_commit("trail", id, after, f"chore: update trail {id} via Atlas Write API")


@mcp.tool()
def list_consolidation_proposals(kind: str = "", limit: int = 50) -> dict[str, Any]:
    """List pending consolidation proposals (memories + trails) from the the consolidation loop queue.

    Read-only. kind: '' (both) | 'memory' | 'trail'. Each memory includes 'memory_id'
    (the id it would receive) and 'already_promoted' (True if that memory already exists
    in Atlas). To promote one, call add_memory(...) / add_trail(...) with its fields —
    those go through the Kernel propose-confirm gate.
    """
    queue_path = Path("outputs/state/consolidation_proposals.json")
    if not queue_path.exists():
        return {"generated_at": None, "memories": [], "trails": [], "note": "no consolidation queue yet"}
    data = json.loads(queue_path.read_text(encoding="utf-8"))
    existing = _get_store().get("memory", {})

    out: dict[str, Any] = {
        "generated_at": data.get("stats", {}).get("generated_at"),
        "stats": data.get("stats", {}),
        "memories": [],
        "trails": [],
    }
    if kind in ("", "memory"):
        for m in data.get("memories", [])[:limit]:
            digest = hashlib.sha256(m["statement"].strip().lower().encode("utf-8")).hexdigest()[:8]
            mid = f"memory-{m['memory_type']}-{digest}"
            out["memories"].append({**m, "memory_id": mid, "already_promoted": mid in existing})
    if kind in ("", "trail"):
        out["trails"] = data.get("trails", [])[:limit]
    return out


@mcp.tool()
def add_session(
    user_id: int,
    source: str = "telegram-bot",
    status: str = "completed",
    lifecycle: str = "active",
    retention_days: int = 30,
    transcript: str = "",
    summary: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    entities_touched: list[str] | None = None,
    project_ids: list[str] | None = None,
    source_request_id: str = "",
    episode_id: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a canonical session entity. Autonomous — writes and commits immediately."""
    if status not in _SESSION_STATUS_VALUES:
        raise ValueError(f"status must be one of: {sorted(_SESSION_STATUS_VALUES)}")
    if lifecycle not in _SESSION_LIFECYCLE_VALUES:
        raise ValueError(f"lifecycle must be one of: {sorted(_SESSION_LIFECYCLE_VALUES)}")
    if retention_days < 1 or retention_days > 3650:
        raise ValueError("retention_days must be between 1 and 3650")

    normalized_source = source.strip() or "telegram-bot"

    store = _get_store()
    sessions = store.get("session", {})
    if source_request_id:
        for existing_session in sessions.values():
            payload = _model_to_dict(existing_session)
            if payload.get("source") == normalized_source and payload.get("source_request_id") == source_request_id:
                return {
                    "ok": True,
                    "idempotent": True,
                    "session_id": payload.get("id"),
                    "session": payload,
                }

    session_id = _next_session_id(normalized_source, source_request_id)
    now = _now_iso()
    data: dict[str, Any] = {
        "id": session_id,
        "source": normalized_source,
        "user_id": user_id,
        "timestamp": now,
        "status": status,
        "lifecycle": lifecycle,
        "retention_days": retention_days,
        "tool_calls": tool_calls or [],
        "entities_touched": entities_touched or [],
        "project_ids": project_ids or [],
        "created_at": now,
        "updated_at": now,
    }
    if transcript:
        data["transcript"] = transcript
    if summary:
        data["summary"] = summary
    if source_request_id:
        data["source_request_id"] = source_request_id
    if episode_id:
        data["episode_id"] = episode_id
    if notes:
        data["notes"] = notes

    result = _write_and_commit("sessions", session_id, data, f"feat: add session {session_id} via Atlas Write API")
    if "error" in result:
        return result
    return {
        "ok": True,
        "idempotent": False,
        "session_id": session_id,
        **result,
    }


@mcp.tool()
def update_session(
    id: str,
    confirm: bool = False,
    status: str = "",
    lifecycle: str = "",
    retention_days: int = 0,
    summary: str = "",
    transcript: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    entities_touched: list[str] | None = None,
    project_ids: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Update fields on an existing session entity via propose-confirm pattern."""
    store = _get_store()
    sessions = store.get("session", {})
    if id not in sessions:
        raise ValueError(f"Session '{id}' not found. Known ids: {sorted(sessions.keys())}")

    rel_path = f"entities/sessions/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if status:
        if status not in _SESSION_STATUS_VALUES:
            raise ValueError(f"status must be one of: {sorted(_SESSION_STATUS_VALUES)}")
        after["status"] = status
    if lifecycle:
        if lifecycle not in _SESSION_LIFECYCLE_VALUES:
            raise ValueError(f"lifecycle must be one of: {sorted(_SESSION_LIFECYCLE_VALUES)}")
        after["lifecycle"] = lifecycle
    if retention_days:
        if retention_days < 1 or retention_days > 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        after["retention_days"] = retention_days
    if summary:
        after["summary"] = summary
    if transcript:
        after["transcript"] = transcript
    if tool_calls is not None:
        after["tool_calls"] = tool_calls
    if entities_touched is not None:
        after["entities_touched"] = entities_touched
    if project_ids is not None:
        after["project_ids"] = project_ids
    if notes:
        after["notes"] = notes

    effective_lifecycle = after.get("lifecycle", "active")
    if effective_lifecycle == "active":
        after.pop("archived_at", None)
        after.pop("pruned_at", None)
    elif effective_lifecycle == "archived":
        after.setdefault("archived_at", _now_iso())
        after.pop("pruned_at", None)
    elif effective_lifecycle == "pruned":
        after.setdefault("pruned_at", _now_iso())
        transcript_value = str(after.get("transcript") or "")
        if transcript_value:
            after["transcript_sha256"] = hashlib.sha256(transcript_value.encode("utf-8")).hexdigest()
            after["transcript"] = ""

    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call update_session(id=..., confirm=True, ...) with the same args to apply.",
        }

    return _write_and_commit("sessions", id, after, f"chore: update session {id} via Atlas Write API")


@mcp.tool()
def archive_session(id: str, confirm: bool = False, reason: str = "") -> dict[str, Any]:
    """Archive a session by setting lifecycle=archived and archived_at timestamp."""
    store = _get_store()
    sessions = store.get("session", {})
    if id not in sessions:
        raise ValueError(f"Session '{id}' not found. Known ids: {sorted(sessions.keys())}")

    rel_path = f"entities/sessions/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    if after.get("lifecycle", "active") == "pruned":
        raise ValueError("Cannot archive a pruned session")

    after["lifecycle"] = "archived"
    after.setdefault("archived_at", _now_iso())
    after.pop("pruned_at", None)
    if reason:
        prior_notes = str(after.get("notes") or "").strip()
        reason_note = f"archive_reason={reason.strip()}"
        after["notes"] = f"{prior_notes}\n{reason_note}".strip()
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call archive_session(id=..., confirm=True, ...) to apply.",
        }

    return _write_and_commit("sessions", id, after, f"chore: archive session {id} via Atlas Write API")


@mcp.tool()
def prune_session(id: str, confirm: bool = False, reason: str = "") -> dict[str, Any]:
    """Prune a session transcript while preserving required audit metadata."""
    store = _get_store()
    sessions = store.get("session", {})
    if id not in sessions:
        raise ValueError(f"Session '{id}' not found. Known ids: {sorted(sessions.keys())}")

    rel_path = f"entities/sessions/{id}.yaml"
    abs_path = REPO_ROOT / rel_path
    current_data: dict[str, Any] = yaml.safe_load(abs_path.read_text(encoding="utf-8")) or {}
    after = dict(current_data)

    lifecycle = str(after.get("lifecycle", "active"))
    if lifecycle not in {"archived", "pruned"}:
        raise ValueError("Session must be archived before pruning")

    transcript_value = str(after.get("transcript") or "")
    if transcript_value:
        after["transcript_sha256"] = hashlib.sha256(transcript_value.encode("utf-8")).hexdigest()
        after["transcript"] = ""

    # Preserve canonical audit metadata even when transcript is removed.
    if not after.get("summary"):
        raise ValueError("Cannot prune session without summary metadata")
    if not after.get("tool_calls"):
        raise ValueError("Cannot prune session without tool_calls metadata")
    if not after.get("entities_touched"):
        raise ValueError("Cannot prune session without entities_touched metadata")

    after["lifecycle"] = "pruned"
    after.setdefault("archived_at", _now_iso())
    after["pruned_at"] = _now_iso()
    if reason:
        after["prune_reason"] = reason.strip()
    after["updated_at"] = _now_iso()

    if not confirm:
        return {
            "action": "preview",
            "id": id,
            "before": current_data,
            "after": after,
            "note": "Call prune_session(id=..., confirm=True, ...) to apply.",
        }

    return _write_and_commit("sessions", id, after, f"chore: prune session {id} via Atlas Write API")


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
DEFAULT_KB_DOC_ROOT = Path("outputs/kb")
DEFAULT_KB_OUTPUT_DIR = DEFAULT_KB_DOC_ROOT / "Projects" / "Atlas" / "40-OUTPUT"
DEFAULT_SERVICES_REPO_ROOT = Path(".")
KB_ROOT_ALLOWED_FILES = {
    "Start Here.md",
    "Standards.md",
    "Project Index.md",
    "The Latest.md",
    "System Log.md",
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
        "GIT_AUTHOR_EMAIL": "atlas@example.local",
        "GIT_COMMITTER_EMAIL": "atlas@example.local",
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
def get_output(name: str) -> str:
    """Read a generated output file from Atlas output directories.

    Pass the filename exactly as it appears (e.g. 'Service Catalog.md',
    'Rules.md', 'Project Index (generated).md'). Returns the file content.
    """
    legacy_outputs_dir = Path(os.environ.get("ATLAS_OUTPUT_DIR", str(DEFAULT_OUTPUTS_DIR))).resolve()
    kb_outputs_dir = Path(os.environ.get("ATLAS_KB_OUTPUT_DIR", str(DEFAULT_KB_OUTPUT_DIR))).resolve()

    search_roots = [kb_outputs_dir, legacy_outputs_dir]
    for root in search_roots:
        target = (root / name).resolve()
        # Guard against path traversal for each root.
        if not str(target).startswith(str(root)):
            raise ValueError(f"Invalid output name: {name!r}")
        if target.exists():
            return target.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"Output '{name}' not found. Searched: {kb_outputs_dir}, {legacy_outputs_dir}. "
        "Run the Atlas pipeline generation step to refresh outputs."
    )


@mcp.tool()
def get_kb_doc(name: str) -> str:
    """Read a knowledge base document from services/docs/kb/.

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
            "Run Track A3 migration to populate services/docs/kb/."
        )
    return target.read_text(encoding="utf-8")


@mcp.tool()
def create_kb_doc(path: str, content: str, confirm: bool = False, commit: bool = True) -> dict[str, Any]:
    """Create a markdown document under services/docs/kb/ via Atlas.

    Single sanctioned KB write path. Uses propose-confirm by default.
    Set confirm=True to apply the write.
    """
    rel, target = _resolve_kb_rel_path(path)
    if not _is_allowed_kb_write(rel):
        raise ValueError(
            "KB write path is not allowed by policy. "
            "Allowed: root canonical docs or Projects/<Project>/<NN-STAGE>/<file>.md"
        )

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
def update_kb_doc(path: str, content: str, confirm: bool = False, commit: bool = True) -> dict[str, Any]:
    """Update a markdown document under services/docs/kb/ via Atlas.

    Single sanctioned KB write path. Uses propose-confirm by default.
    Set confirm=True to apply the write.
    """
    rel, target = _resolve_kb_rel_path(path)
    if not _is_allowed_kb_write(rel):
        raise ValueError(
            "KB write path is not allowed by policy. "
            "Allowed: root canonical docs or Projects/<Project>/<NN-STAGE>/<file>.md"
        )

    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"KB doc not found: {rel.as_posix()}")

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


def _kb_replace_section_text(text: str, anchor: str, new_body: str, create_missing: bool) -> str:
    """Replace the body of the section whose heading matches `anchor` (heading
    kept, replaced up to the next heading of same-or-shallower depth). Raises
    ValueError on a missing (unless create_missing) or ambiguous anchor."""
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
                       expected_hash: str = "", confirm: bool = False, commit: bool = True) -> dict[str, Any]:
    """Replace one markdown section's body in a KB doc, leaving the rest intact.

    `anchor` is the heading text (with or without leading #). The matched heading
    line is kept; its body is replaced up to the next heading of same-or-shallower
    depth. The caller supplies ONLY the new section body — no full-file round-trip,
    so the rest of the doc cannot be truncated.

    - anchor not found -> error listing available headings, unless
      create_missing=True (then a new `## anchor` section is appended).
    - anchor matches >1 heading -> error with line numbers (disambiguate).
    expected_hash: optional optimistic guard (see append_kb_doc).
    Propose-confirm: call with confirm=True to apply.
    """
    rel, target = _kb_resolve_existing(path)
    compute = lambda t: _kb_replace_section_text(t, anchor, content, create_missing)
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
    PROBE_CACHE = Path("outputs/state/atlas_probe_latest.json")

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

        # P3 — keep 127.0.0.1 callers (loopback agents/automations) working by injecting a
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
