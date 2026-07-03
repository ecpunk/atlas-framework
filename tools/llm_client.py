from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_FALLBACK_MODELS = ("claude-sonnet-4-6", "claude-sonnet-4-5")
_MODEL_PRICING_USD_PER_MTOKEN = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
}
_DEFAULT_MAX_USD = 1.00
_DEFAULT_DAILY_MAX_USD = 3.00
_DEFAULT_CALLER_ID = "atlas-plan-check"
_DEFAULT_SHARED_LEDGER = "outputs/state/llm_cost_gate.json"

_LOGGER = logging.getLogger("atlas.llm_client")
if not _LOGGER.handlers:
    _HANDLER = logging.StreamHandler(sys.stderr)
    _HANDLER.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    _LOGGER.addHandler(_HANDLER)
_LOGGER.setLevel(logging.INFO)


def _load_positive_float(name: str, default: float) -> float:
    raw_value = (os.environ.get(name) or "").strip()
    if not raw_value:
        return default

    try:
        parsed = float(raw_value)
    except ValueError:
        _LOGGER.warning("Invalid %s=%r; falling back to %.2f", name, raw_value, default)
        return default

    if parsed <= 0:
        _LOGGER.warning("Non-positive %s=%.4f; falling back to %.2f", name, parsed, default)
        return default

    return parsed


def _load_bool(name: str, default: bool) -> bool:
    raw_value = (os.environ.get(name) or "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    _LOGGER.warning("Invalid boolean %s=%r; falling back to %s", name, raw_value, default)
    return default


def _load_int(name: str, default: int) -> int:
    raw_value = (os.environ.get(name) or "").strip()
    if not raw_value:
        return default

    try:
        parsed = int(raw_value)
    except ValueError:
        _LOGGER.warning("Invalid integer %s=%r; falling back to %d", name, raw_value, default)
        return default

    if parsed < 0:
        _LOGGER.warning("Negative %s=%d; falling back to %d", name, parsed, default)
        return default

    return parsed


_ATLAS_LLM_MAX_USD = _load_positive_float("ATLAS_LLM_MAX_USD", _DEFAULT_MAX_USD)
_ATLAS_LLM_DAILY_MAX_USD = _load_positive_float("ATLAS_LLM_DAILY_MAX_USD", _DEFAULT_DAILY_MAX_USD)
_ATLAS_LLM_GLOBAL_DAILY_MAX_USD = _load_positive_float(
    "ATLAS_LLM_GLOBAL_DAILY_MAX_USD",
    _ATLAS_LLM_DAILY_MAX_USD,
)
_ATLAS_LLM_MAX_CALLS_PER_RUN = _load_int("ATLAS_LLM_MAX_CALLS_PER_RUN", 0)
_ATLAS_LLM_CALLER_ID = (os.environ.get("ATLAS_LLM_CALLER_ID") or _DEFAULT_CALLER_ID).strip() or _DEFAULT_CALLER_ID

_cost_tracker: dict[str, int | float] = {"tokens_in": 0, "tokens_out": 0, "est_usd": 0.0}

_SERVICES_LIB = Path(
    os.environ.get(
        "ATLAS_SHARED_AUTOMATIONS_LIB",
        "outputs/lib",
    )
)
if _SERVICES_LIB.exists() and str(_SERVICES_LIB) not in sys.path:
    sys.path.insert(0, str(_SERVICES_LIB))

try:
    from llm_cost_gate import LlmCostPolicyGate  # type: ignore[import-not-found]
except Exception as exc:  # noqa: BLE001
    LlmCostPolicyGate = None  # type: ignore[assignment,misc]
    _LOGGER.warning("Shared llm_cost_gate import failed; using process-only cap fallback: %s", exc)


def _build_cost_gate() -> Any | None:
    if LlmCostPolicyGate is None:
        return None

    policy = {
        "enabled": _load_bool("ATLAS_LLM_COST_GATE_ENABLED", True),
        "enforce": _load_bool("ATLAS_LLM_COST_GATE_ENFORCE", True),
        "provider": "anthropic",
        "global_daily_max_usd": _ATLAS_LLM_GLOBAL_DAILY_MAX_USD,
        "caller_daily_max_usd": _ATLAS_LLM_DAILY_MAX_USD,
        "caller_run_max_usd": _ATLAS_LLM_MAX_USD,
        "caller_max_calls_per_run": _ATLAS_LLM_MAX_CALLS_PER_RUN,
        "model_pricing_usd_per_mtoken": _MODEL_PRICING_USD_PER_MTOKEN,
    }
    ledger_path = os.environ.get("ATLAS_LLM_SPEND_STATE_FILE", _DEFAULT_SHARED_LEDGER)

    gate = LlmCostPolicyGate(
        caller_id=_ATLAS_LLM_CALLER_ID,
        policy=policy,
        state_file=ledger_path,
        logger=_LOGGER,
    )
    _LOGGER.info(
        "Loaded shared LLM cost gate caller=%s enforce=%s run_cap=%.2f caller_daily=%.2f global_daily=%.2f ledger=%s",
        _ATLAS_LLM_CALLER_ID,
        gate.enforce,
        _ATLAS_LLM_MAX_USD,
        _ATLAS_LLM_DAILY_MAX_USD,
        _ATLAS_LLM_GLOBAL_DAILY_MAX_USD,
        ledger_path,
    )
    return gate


_COST_GATE = _build_cost_gate()


def _model_candidates() -> list[str]:
    override = (os.environ.get("ANTHROPIC_MODEL") or "").strip()
    if not override:
        return list(_FALLBACK_MODELS)

    ordered = [override]
    for model in _FALLBACK_MODELS:
        if model != override:
            ordered.append(model)
    return ordered


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _parse_response_json(text: str) -> dict[str, Any]:
    payload = json.loads(_strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError("LLM response is not a JSON object")

    fires = bool(payload.get("fires", False))
    evidence_raw = payload.get("evidence", "")
    evidence = str(evidence_raw).strip() if evidence_raw is not None else ""

    suggested_fix_raw = payload.get("suggested_fix", None)
    if suggested_fix_raw is None:
        suggested_fix = None
    else:
        suggested_text = str(suggested_fix_raw).strip()
        suggested_fix = suggested_text if suggested_text else None

    return {
        "fires": fires,
        "evidence": evidence,
        "suggested_fix": suggested_fix,
    }


def _pricing_for_model(model: str) -> dict[str, float]:
    return _MODEL_PRICING_USD_PER_MTOKEN.get(model, _MODEL_PRICING_USD_PER_MTOKEN[_FALLBACK_MODELS[0]])


def _estimate_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    pricing = _pricing_for_model(model)
    input_cost = (max(tokens_in, 0) / 1_000_000) * pricing["input"]
    output_cost = (max(tokens_out, 0) / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def _estimate_call_cost(input_tokens_estimate: int, model: str) -> float:
    pricing = _pricing_for_model(model)
    return (max(input_tokens_estimate, 0) / 1_000_000) * pricing["input"]


def get_cost_summary() -> dict[str, int | float]:
    return {
        "tokens_in": int(_cost_tracker["tokens_in"]),
        "tokens_out": int(_cost_tracker["tokens_out"]),
        "est_usd": float(_cost_tracker["est_usd"]),
        "max_usd": float(_ATLAS_LLM_MAX_USD),
    }


def get_daily_spend_summary() -> dict:
    if _COST_GATE is None:
        return {"date": "", "accumulated_usd": 0.0, "call_count": 0}

    summary = _COST_GATE.get_daily_summary()
    return {
        "date": summary.get("date", ""),
        "accumulated_usd": float(summary.get("caller_usd", 0.0)),
        "call_count": int(summary.get("caller_calls", 0)),
        "global_accumulated_usd": float(summary.get("global_usd", 0.0)),
        "global_call_count": int(summary.get("global_calls", 0)),
    }


def _cost_cap_result(model: str, evidence: str) -> dict[str, Any]:
    return {
        "fires": False,
        "evidence": evidence,
        "suggested_fix": None,
        "model": model,
        "tokens_in": 0,
        "tokens_out": 0,
        "error": True,
        "cost_capped": True,
    }



def get_llm_runtime_info() -> dict[str, Any]:
    """Return LLM runtime model/cost state for operators."""
    spend_state_file = os.environ.get("ATLAS_LLM_SPEND_STATE_FILE", _DEFAULT_SHARED_LEDGER)
    return {
        "model_candidates": _model_candidates(),
        "caller_id": _ATLAS_LLM_CALLER_ID,
        "spend_state_file": spend_state_file,
        "shared_gate_loaded": _COST_GATE is not None,
        "shared_gate_enabled": bool(getattr(_COST_GATE, "enabled", False)),
        "shared_gate_enforce": bool(getattr(_COST_GATE, "enforce", False)),
        "daily_spend": get_daily_spend_summary(),
        "run_spend": get_cost_summary(),
    }


def evaluate_rule(system_prompt: str, user_content: str, rule_id: str) -> dict[str, Any]:
    models = _model_candidates()
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        result = {
            "fires": False,
            "evidence": "LLM error: ANTHROPIC_API_KEY not set",
            "suggested_fix": None,
            "model": models[0],
            "tokens_in": 0,
            "tokens_out": 0,
            "error": True,
            "cost_capped": False,
        }
        _LOGGER.info("rule_id=%s fires=%s error=%s model=%s", rule_id, False, True, result["model"])
        return result

    estimated_input_tokens = int((len(system_prompt) + len(user_content)) / 4)

    if _COST_GATE is not None:
        gate_decision = _COST_GATE.preflight(
            model=models[0],
            provider="anthropic",
            input_tokens_estimate=estimated_input_tokens,
        )
        if gate_decision.would_block:
            _LOGGER.warning("rule_id=%s shadow_allow model=%s reason=%s", rule_id, models[0], gate_decision.reason)
        if not gate_decision.allowed:
            result = _cost_cap_result(models[0], gate_decision.reason)
            _LOGGER.info(
                "rule_id=%s fires=%s error=%s cost_capped=%s (shared-gate) model=%s",
                rule_id,
                False,
                True,
                True,
                models[0],
            )
            return result
    else:
        projected_cost = float(_cost_tracker["est_usd"]) + _estimate_call_cost(estimated_input_tokens, models[0])
        if projected_cost > _ATLAS_LLM_MAX_USD:
            result = _cost_cap_result(
                models[0],
                "Cost cap exceeded: "
                f"${float(_cost_tracker['est_usd']):.2f} accumulated, cap ${_ATLAS_LLM_MAX_USD:.2f}",
            )
            _LOGGER.info(
                "rule_id=%s fires=%s error=%s cost_capped=%s (fallback) model=%s",
                rule_id,
                False,
                True,
                True,
                models[0],
            )
            return result

    client = Anthropic(api_key=api_key)
    last_error: Exception | None = None

    for model in models:
        if _COST_GATE is not None and model != models[0]:
            gate_decision = _COST_GATE.preflight(
                model=model,
                provider="anthropic",
                input_tokens_estimate=estimated_input_tokens,
            )
            if gate_decision.would_block:
                _LOGGER.warning("rule_id=%s shadow_allow model=%s reason=%s", rule_id, model, gate_decision.reason)
            if not gate_decision.allowed:
                result = _cost_cap_result(model, gate_decision.reason)
                _LOGGER.info(
                    "rule_id=%s fires=%s error=%s cost_capped=%s (shared-gate) model=%s",
                    rule_id,
                    False,
                    True,
                    True,
                    model,
                )
                return result
        elif _COST_GATE is None:
            projected_cost = float(_cost_tracker["est_usd"]) + _estimate_call_cost(estimated_input_tokens, model)
            if projected_cost > _ATLAS_LLM_MAX_USD:
                result = _cost_cap_result(
                    model,
                    "Cost cap exceeded: "
                    f"${float(_cost_tracker['est_usd']):.2f} accumulated, cap ${_ATLAS_LLM_MAX_USD:.2f}",
                )
                _LOGGER.info(
                    "rule_id=%s fires=%s error=%s cost_capped=%s (fallback) model=%s",
                    rule_id,
                    False,
                    True,
                    True,
                    model,
                )
                return result

        try:
            response = client.messages.create(
                model=model,
                max_tokens=300,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            parsed = _parse_response_json(_extract_text(response))
            usage = getattr(response, "usage", None)
            tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
            call_est_usd = _estimate_cost(tokens_in, tokens_out, model)

            _cost_tracker["tokens_in"] = int(_cost_tracker["tokens_in"]) + tokens_in
            _cost_tracker["tokens_out"] = int(_cost_tracker["tokens_out"]) + tokens_out
            _cost_tracker["est_usd"] = float(_cost_tracker["est_usd"]) + call_est_usd

            if _COST_GATE is not None:
                _COST_GATE.record_usage(
                    model=model,
                    provider="anthropic",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )

            result = {
                "fires": bool(parsed["fires"]),
                "evidence": str(parsed["evidence"]),
                "suggested_fix": parsed["suggested_fix"],
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": False,
                "cost_capped": False,
            }
            _LOGGER.info(
                "rule_id=%s fires=%s error=%s model=%s",
                rule_id,
                result["fires"],
                result["error"],
                model,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    message = f"LLM error: {last_error}" if last_error is not None else "LLM error: unknown error"
    model_name = models[0]
    result = {
        "fires": False,
        "evidence": message,
        "suggested_fix": None,
        "model": model_name,
        "tokens_in": 0,
        "tokens_out": 0,
        "error": True,
        "cost_capped": False,
    }
    _LOGGER.info("rule_id=%s fires=%s error=%s model=%s", rule_id, False, True, model_name)
    return result


def _parse_intent_response_json(text: str) -> dict[str, Any]:
    payload = json.loads(_strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError("LLM response is not a JSON object")

    intent = str(payload.get("intent", "unknown")).strip().lower()
    if intent not in {"server_status", "docker_status", "service_logs", "memory_check", "docker_action", "unknown", "clarify"}:
        intent = "unknown"

    side_effect = str(payload.get("side_effect", "none")).strip().lower()
    if side_effect not in {"none", "read", "write"}:
        side_effect = "none"

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    action = str(payload.get("action", "")).strip().lower()
    if action not in {"", "restart", "start", "stop"}:
        action = ""

    container = str(payload.get("container", "")).strip().lower()
    service = str(payload.get("service", "")).strip().lower()

    try:
        lines = int(payload.get("lines", 50))
    except (TypeError, ValueError):
        lines = 50
    lines = max(1, min(lines, 500))

    reason = str(payload.get("reason", "")).strip()

    return {
        "intent": intent,
        "side_effect": side_effect,
        "confidence": confidence,
        "action": action,
        "container": container,
        "service": service,
        "lines": lines,
        "reason": reason,
    }


def route_telegram_intent(user_text: str, context_hint: str = "") -> dict[str, Any]:
    system_prompt = (
        "You are an intent router for a Telegram homelab ops bot. "
        "Return ONLY JSON with keys: intent, side_effect, confidence, action, container, service, lines, reason. "
        "Allowed intent: server_status, docker_status, service_logs, memory_check, docker_action, unknown, clarify. "
        "Allowed side_effect: none, read, write. "
        "For docker_action intent, action must be restart/start/stop and include container. "
        "For service_logs intent, include service and optional lines (1-500, default 50). "
        "Do not include markdown or prose outside JSON."
    )

    user_content = f"User text: {user_text}\\nContext: {context_hint or 'none'}"
    models = _model_candidates()
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {
            "intent": "unknown",
            "side_effect": "none",
            "confidence": 0.0,
            "action": "",
            "container": "",
            "service": "",
            "lines": 50,
            "reason": "LLM error: ANTHROPIC_API_KEY not set",
            "model": models[0],
            "tokens_in": 0,
            "tokens_out": 0,
            "error": True,
            "cost_capped": False,
        }

    estimated_input_tokens = int((len(system_prompt) + len(user_content)) / 4)
    if _COST_GATE is not None:
        gate_decision = _COST_GATE.preflight(
            model=models[0],
            provider="anthropic",
            input_tokens_estimate=estimated_input_tokens,
        )
        if gate_decision.would_block:
            _LOGGER.warning("telegram-route shadow_allow model=%s reason=%s", models[0], gate_decision.reason)
        if not gate_decision.allowed:
            return {
                "intent": "unknown",
                "side_effect": "none",
                "confidence": 0.0,
                "action": "",
                "container": "",
                "service": "",
                "lines": 50,
                "reason": gate_decision.reason,
                "model": models[0],
                "tokens_in": 0,
                "tokens_out": 0,
                "error": True,
                "cost_capped": True,
            }

    client = Anthropic(api_key=api_key)
    last_error: Exception | None = None
    for model in models:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=350,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            parsed = _parse_intent_response_json(_extract_text(response))
            usage = getattr(response, "usage", None)
            tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
            call_est_usd = _estimate_cost(tokens_in, tokens_out, model)

            _cost_tracker["tokens_in"] = int(_cost_tracker["tokens_in"]) + tokens_in
            _cost_tracker["tokens_out"] = int(_cost_tracker["tokens_out"]) + tokens_out
            _cost_tracker["est_usd"] = float(_cost_tracker["est_usd"]) + call_est_usd
            if _COST_GATE is not None:
                _COST_GATE.record_usage(
                    model=model,
                    provider="anthropic",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )

            return {
                **parsed,
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": False,
                "cost_capped": False,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    return {
        "intent": "unknown",
        "side_effect": "none",
        "confidence": 0.0,
        "action": "",
        "container": "",
        "service": "",
        "lines": 50,
        "reason": f"LLM error: {last_error}" if last_error else "LLM error: unknown error",
        "model": models[0],
        "tokens_in": 0,
        "tokens_out": 0,
        "error": True,
        "cost_capped": False,
    }


# ---------------------------------------------------------------------------
# Freeform agentic responder — wraps a multi-turn Anthropic tool-use loop
# that can call hydra_ops_mcp (live ops) and atlas-mcp (internal) tools.
# ---------------------------------------------------------------------------

_OPS_MCP_URL = "http://127.0.0.1:8103/mcp"
_FREEFORM_MAX_ITERS = 5
_FREEFORM_MAX_TOKENS = 2048

_FREEFORM_SYSTEM = (
    "You are the homelab ops assistant for your Hydra homelab server. "
    "The operator is a principal security engineer — be terse, technical, and direct. "
    "Answer questions by using the available tools to fetch live data. "
    "Never guess at container names, service IDs, or port numbers — look them up. "
    "Do not use markdown headers. Prefer concise prose over bullet lists unless "
    "the data is genuinely tabular. Keep final answers under 3000 characters."
)

# Read-only ops tools exposed to the freeform agent (subset of hydra_ops_mcp)
_OPS_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "ops_server_status",
        "description": "Get live health status for all monitored services/hosts.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ops_docker_status",
        "description": "List all Docker containers with their running state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ops_service_logs",
        "description": "Fetch recent logs for a named service or container.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service or container name"},
                "lines": {"type": "integer", "description": "Number of lines (1-200, default 50)", "default": 50},
            },
            "required": ["service_name"],
        },
    },
]

# Atlas read-only tools callable directly as Python functions
_ATLAS_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "atlas_stack_summary",
        "description": "Get a high-level summary of entity counts (projects, services, tasks, sessions).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "atlas_list_services",
        "description": "List all services in the service catalog. Optional filter by lifecycle or category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lifecycle": {"type": "string", "description": "Filter: active, retired, planned"},
                "category": {"type": "string", "description": "Filter by category string"},
            },
            "required": [],
        },
    },
    {
        "name": "atlas_get_service",
        "description": "Get full details for a single service by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Service ID"}},
            "required": ["id"],
        },
    },
    {
        "name": "atlas_list_projects",
        "description": "List all projects. Optional filter by category or status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "atlas_get_project",
        "description": "Get full details for a project including phase, goals, and open tasks.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Project ID"}},
            "required": ["id"],
        },
    },
    {
        "name": "atlas_check_drift",
        "description": "Run or read cached reality probes against running services and report drift.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string", "description": "Scope to one service (omit for all)"},
                "force": {"type": "boolean", "description": "Force live re-probe instead of reading cache"},
            },
            "required": [],
        },
    },
    {
        "name": "atlas_session_activity_summary",
        "description": "Summarize recent session activity from the last N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since_hours": {"type": "integer", "description": "Hours to look back (default 168 = 7 days)"},
                "source": {"type": "string", "description": "Filter by source agent"},
            },
            "required": [],
        },
    },
]

_ALL_FREEFORM_TOOLS = _OPS_TOOL_SCHEMAS + _ATLAS_TOOL_SCHEMAS


def _ops_mcp_call(tool_name: str, arguments: dict) -> Any:
    """Call a read-only tool on hydra_ops_mcp via JSON-RPC HTTP."""
    import requests as _req

    actual_name = tool_name[len("ops_"):]  # "ops_server_status" → "server_status"
    _session_id: list[str | None] = [None]
    _next_id: list[int] = [1]

    def _headers() -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if _session_id[0]:
            h["Mcp-Session-Id"] = _session_id[0]
        return h

    def _extract(resp: Any) -> dict:
        text = resp.text or ""
        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" in ctype and text.strip().startswith("{"):
            return resp.json()
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            candidate = line[6:].strip()
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if "result" in parsed or "error" in parsed:
                return parsed
        raise RuntimeError(f"Cannot parse MCP response: status={resp.status_code}")

    def _rpc(method: str, params: dict | None, has_result: bool) -> dict | None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if has_result:
            payload["id"] = _next_id[0]
            _next_id[0] += 1
        resp = _req.post(_OPS_MCP_URL, headers=_headers(), json=payload, timeout=10)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            _session_id[0] = sid
        if not has_result:
            resp.raise_for_status()
            return None
        parsed = _extract(resp)
        if "error" in parsed:
            raise RuntimeError(f"MCP error {method}: {parsed['error']}")
        return parsed.get("result")

    _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "atlas-freeform-agent", "version": "0.1.0"},
    }, has_result=True)
    _rpc("notifications/initialized", None, has_result=False)

    result = _rpc("tools/call", {"name": actual_name, "arguments": arguments}, has_result=True)
    if not result:
        return None
    content = result.get("content") or []
    if not content:
        return result
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _atlas_call(tool_name: str, arguments: dict) -> Any:
    """Dispatch an atlas tool by calling the mcp_server function directly."""
    import tools.mcp_server as _srv  # late import avoids circular dependency

    fn_map = {
        "atlas_stack_summary": lambda a: _srv.stack_summary(),
        "atlas_list_services": lambda a: _srv.list_services(**a),
        "atlas_get_service": lambda a: _srv.get_service(**a),
        "atlas_list_projects": lambda a: _srv.list_projects(**a),
        "atlas_get_project": lambda a: _srv.get_project(**a),
        "atlas_check_drift": lambda a: _srv.check_drift(**a),
        "atlas_session_activity_summary": lambda a: _srv.session_activity_summary(**a),
    }
    fn = fn_map.get(tool_name)
    if fn is None:
        return {"error": f"Unknown atlas tool: {tool_name}"}
    return fn(arguments)


def freeform_respond(user_text: str, context_hint: str = "") -> dict[str, Any]:
    """Run a freeform agentic loop: Sonnet + MCP tools for unknown/open-ended queries.

    Returns {"response": str, "model": str, "tokens_in": int, "tokens_out": int,
             "tool_calls": int, "error": bool, "cost_capped": bool}.
    """
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return {
            "response": "LLM unavailable: ANTHROPIC_API_KEY not set.",
            "model": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "tool_calls": 0,
            "error": True,
            "cost_capped": False,
        }

    models = _model_candidates()
    model = models[0]

    if _COST_GATE is not None:
        gate_decision = _COST_GATE.preflight(
            model=model,
            provider="anthropic",
            input_tokens_estimate=800,
        )
        if not gate_decision.allowed:
            return {
                "response": f"Cost gate blocked this request: {gate_decision.reason}",
                "model": model,
                "tokens_in": 0,
                "tokens_out": 0,
                "tool_calls": 0,
                "error": True,
                "cost_capped": True,
            }

    client = Anthropic(api_key=api_key)

    user_content = user_text.strip()
    if context_hint:
        user_content = f"{user_content}\n\nContext: {context_hint}"

    messages: list[dict] = [{"role": "user", "content": user_content}]
    total_tokens_in = 0
    total_tokens_out = 0
    total_tool_calls = 0
    final_text = "No response generated."

    try:
        for _ in range(_FREEFORM_MAX_ITERS):
            response = client.messages.create(
                model=model,
                max_tokens=_FREEFORM_MAX_TOKENS,
                system=_FREEFORM_SYSTEM,
                tools=_ALL_FREEFORM_TOOLS,  # type: ignore[arg-type]
                messages=messages,
            )
            usage = getattr(response, "usage", None)
            tok_in = int(getattr(usage, "input_tokens", 0) or 0)
            tok_out = int(getattr(usage, "output_tokens", 0) or 0)
            total_tokens_in += tok_in
            total_tokens_out += tok_out

            if response.stop_reason == "end_turn":
                final_text = _extract_text(response)
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    total_tool_calls += 1
                    t_name: str = block.name
                    t_input: dict = block.input or {}
                    try:
                        if t_name.startswith("ops_"):
                            raw = _ops_mcp_call(t_name, t_input)
                        else:
                            raw = _atlas_call(t_name, t_input)
                        result_text = json.dumps(raw, default=str)
                    except Exception as tool_exc:  # noqa: BLE001
                        result_text = json.dumps({"error": str(tool_exc)})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            final_text = _extract_text(response) or f"(stopped: {response.stop_reason})"
            break
        else:
            final_text = "Reached max reasoning depth without a final answer."

        _cost_tracker["tokens_in"] = int(_cost_tracker["tokens_in"]) + total_tokens_in
        _cost_tracker["tokens_out"] = int(_cost_tracker["tokens_out"]) + total_tokens_out
        _cost_tracker["est_usd"] = float(_cost_tracker["est_usd"]) + _estimate_cost(
            total_tokens_in, total_tokens_out, model
        )
        if _COST_GATE is not None:
            _COST_GATE.record_usage(
                model=model,
                provider="anthropic",
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
            )

        return {
            "response": final_text,
            "model": model,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "tool_calls": total_tool_calls,
            "error": False,
            "cost_capped": False,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "response": f"Agent error: {exc}",
            "model": model,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "tool_calls": total_tool_calls,
            "error": True,
            "cost_capped": False,
        }
