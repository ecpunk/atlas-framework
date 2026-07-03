"""
Generator: health_summary
Produces apps/dashboard/health-summary.json — the Atlas 10k-ft ops brief.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

NAME = "health_summary"
INPUTS: list[str] = []
OUTPUTS = ["outputs/dashboard/health-summary.json"]

# ── Paths ────────────────────────────────────────────────────────────────────
SERVICES_ROOT = Path(".")
ATLAS_ROOT = Path(".")
ENTITIES_ROOT = ATLAS_ROOT / "entities"
STATE_ROOT = SERVICES_ROOT / "automations/state"
KB_ROOT = SERVICES_ROOT / "docs/kb"
DASHBOARD_ROOT = SERVICES_ROOT / "apps/dashboard"

# Tier-1: degraded/missing = critical (red), not just warn
TIER1 = {"auth-gate", "ops-dashboard", "atlas-mcp", "host-nginx-proxy"}

BUDGET_THRESHOLD = 0.80

# (display_name, timer_unit, warn_after_seconds)
LOOP_TIMERS = [
    ("probe",     "atlas-probe.timer",                    30 * 60),
    ("smoke",     "atlas-mcp-smoke.timer",                20 * 60),
    ("budget",    "atlas-resource-budget-monitor.timer",  60 * 60),
    ("drift",     "atlas-drift.timer",                    30 * 3600),
    ("dashboard", "dashboard-status.timer",               15 * 60),
]

LOOP_ICONS = {
    "probe":     "🔍",
    "smoke":     "💨",
    "budget":    "💰",
    "drift":     "🌊",
    "dashboard": "📊",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _load_json(path) -> dict | list | None:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _timer_last_run(timer_name: str) -> tuple[str | None, int | None]:
    """Return (iso_timestamp, age_seconds) for a systemd timer's last trigger."""
    try:
        r = subprocess.run(
            ["systemctl", "show", timer_name, "--property=LastTriggerUSec", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.strip()
        if not val or val in ("n/a", "0"):
            return None, None
        dt = datetime.strptime(val, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - dt).total_seconds())
        return dt.isoformat().replace("+00:00", "Z"), age
    except Exception:
        return None, None


def _get_syslog(n: int = 60) -> list[dict]:
    """Pull last N journal lines from atlas-related services as structured records."""
    units = [
        "atlas-probe.service",
        "atlas-mcp-smoke.service",
        "atlas-drift.service",
        "atlas-resource-budget-monitor.service",
        "atlas-mcp.service",
        "dashboard-status.service",
    ]
    args = ["journalctl", "--no-pager", "-n", str(n), "--output=json"]
    for u in units:
        args += ["-u", u]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception:
        return []

    entries = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts_us = obj.get("__REALTIME_TIMESTAMP", "0")
        try:
            dt = datetime.fromtimestamp(int(ts_us) / 1e6, tz=timezone.utc)
            ts = dt.strftime("%H:%M:%S")
        except Exception:
            ts = "?"
        unit = obj.get("_SYSTEMD_UNIT", obj.get("SYSLOG_IDENTIFIER", "?"))
        unit = unit.replace(".service", "")
        msg = obj.get("MESSAGE", "")
        if not isinstance(msg, str):
            msg = str(msg)
        msg = msg.strip()
        if not msg:
            continue
        msg_up = msg.upper()
        if any(w in msg_up for w in ("FAIL", "ERROR", "CRITICAL", "500")):
            level = "error"
        elif any(w in msg_up for w in ("WARN", "WARNING")):
            level = "warn"
        elif any(w in msg_up for w in ("PASS", "RESULT PASS", "OK", "DONE")):
            level = "ok"
        else:
            level = "info"
        entries.append({"ts": ts, "unit": unit, "level": level, "msg": msg[:250]})
    return entries


def _pstatus(p: dict) -> str:
    s = p.get("status", "unknown")
    return s.split(":")[-1] if ":" in s else s


def _plc(s: dict) -> str:
    lc = s.get("lifecycle", "unknown")
    return lc.split(":")[-1] if ":" in lc else lc


# ── Main generate ─────────────────────────────────────────────────────────────

def generate(store: dict) -> dict[str, str]:  # noqa: ARG001
    now = _now_ts()

    # ── Stack: service probe results ──────────────────────────────────────────
    probe = _load_json(STATE_ROOT / "atlas_probe_latest.json") or {}
    results = probe.get("results", [])
    total_probed = len(results)
    service_issues = []
    ok_count = 0
    for r in results:
        probes = r.get("probes", [])
        all_pass = all(p.get("pass", True) for p in probes)
        drifted = r.get("drift", False)
        if not all_pass or drifted:
            failing = [p["type"] for p in probes if not p.get("pass", True)]
            service_issues.append({
                "id": r["service_id"],
                "label": r.get("name", r["service_id"]),
                "health": "degraded" if drifted else "unknown",
                "failing_probes": failing,
                "tier1": r["service_id"] in TIER1,
            })
        else:
            ok_count += 1

    tier1_down = any(i["tier1"] for i in service_issues)
    stack_status = "critical" if tier1_down else ("warn" if service_issues else "ok")

    # ── Resources: budget pressure ────────────────────────────────────────────
    spikes = _load_json(STATE_ROOT / "resource_budget_spikes.json") or {}
    pressure = []
    for svc_id, data in spikes.items():
        pct = data.get("last_pct", 0)
        if pct >= BUDGET_THRESHOLD:
            pressure.append({
                "id": svc_id,
                "rss_mb": data.get("last_rss_mb", 0),
                "pct": round(pct * 100),
                "consecutive": data.get("consecutive", 0),
            })
    pressure.sort(key=lambda x: x["pct"], reverse=True)

    # ── Work: tasks + projects ────────────────────────────────────────────────
    tasks = []
    for f in (ENTITIES_ROOT / "tasks").glob("*.yaml"):
        try:
            tasks.append(yaml.safe_load(f.read_text()))
        except Exception:
            pass

    open_tasks = [t for t in tasks if t.get("status") not in ("resolved", "deferred")]
    open_by_pri = Counter(t.get("priority", "medium") for t in open_tasks)
    high_open = open_by_pri.get("high", 0)
    work_status = "critical" if high_open > 5 else ("warn" if high_open > 0 else "ok")

    _pri_rank = {"high": 0, "medium": 1, "low": 2}
    open_tasks_detail = sorted(
        [{"id": t.get("id", ""), "title": t.get("title", "?"), "priority": t.get("priority", "medium")}
         for t in open_tasks],
        key=lambda x: _pri_rank.get(x["priority"], 1),
    )

    projects = []
    for f in (ENTITIES_ROOT / "projects").glob("*.yaml"):
        try:
            projects.append(yaml.safe_load(f.read_text()))
        except Exception:
            pass
    proj_by_status = Counter(_pstatus(p) for p in projects)

    # Last session timestamp
    session_files = sorted(
        (ENTITIES_ROOT / "sessions").glob("*.yaml"),
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    last_session_at = None
    if session_files:
        try:
            s = yaml.safe_load(session_files[0].read_text())
            last_session_at = s.get("ended_at") or s.get("started_at") or s.get("created_at")
        except Exception:
            pass

    # ── Atlas loops ───────────────────────────────────────────────────────────
    loops = []
    for name, timer, warn_secs in LOOP_TIMERS:
        iso, age = _timer_last_run(timer)
        if age is None:
            loop_status = "unknown"
            age_str = "never"
        elif age > warn_secs:
            loop_status = "warn"
            age_str = _humanize(age)
        else:
            loop_status = "ok"
            age_str = _humanize(age)
        loops.append({
            "name": name,
            "icon": LOOP_ICONS.get(name, "⚙️"),
            "timer": timer,
            "last_run": iso,
            "age": age_str,
            "status": loop_status,
        })
    atlas_status = "warn" if any(l["status"] != "ok" for l in loops) else "ok"

    # ── Inventory ─────────────────────────────────────────────────────────────
    service_files = list((ENTITIES_ROOT / "services").glob("*.yaml"))
    svc_by_lc: Counter = Counter()
    for f in service_files:
        try:
            d = yaml.safe_load(f.read_text())
            svc_by_lc[_plc(d)] += 1
        except Exception:
            pass

    def _count(subdir: str) -> int:
        p = ENTITIES_ROOT / subdir
        return len(list(p.glob("*.yaml"))) if p.exists() else 0

    kb_docs = len(list(KB_ROOT.rglob("*.md"))) if KB_ROOT.exists() else 0
    vocab_count = len(list((ATLAS_ROOT / "vocabularies").glob("*.yaml"))) \
        if (ATLAS_ROOT / "vocabularies").exists() else 0
    llm_cost = _load_json(STATE_ROOT / "llm_cost_gate.json") or {}
    llm_30d = llm_cost.get("global", {}).get("usd", None)

    inventory = {
        "services_total": len(service_files),
        "services_by_lifecycle": dict(svc_by_lc),
        "tasks_total": len(tasks),
        "tasks_open": len(open_tasks),
        "tasks_resolved": len([t for t in tasks if t.get("status") == "resolved"]),
        "projects_total": len(projects),
        "projects_by_status": dict(proj_by_status),
        "rules": _count("rules"),
        "sessions": _count("sessions"),
        "kb_docs": kb_docs,
        "vocab_files": vocab_count,
        "skills": _count("skills"),
        "agents": _count("agents"),
        "consumer_profiles": _count("consumer_profiles"),
        "llm_usd_today": round(llm_30d, 4) if llm_30d is not None else None,
    }

    # ── Syslog ────────────────────────────────────────────────────────────────
    syslog = _get_syslog(60)

    # ── Overall status (worst wins) ───────────────────────────────────────────
    rank = {"ok": 0, "warn": 1, "critical": 2, "unknown": 1}
    overall_rank = max(rank.get(s, 0) for s in [stack_status, work_status, atlas_status,
                                                  "warn" if pressure else "ok"])
    overall = ["ok", "warn", "critical"][min(overall_rank, 2)]

    summary = {
        "generated_at": now,
        "overall": overall,
        "stack": {
            "total": total_probed,
            "ok": ok_count,
            "issues_count": len(service_issues),
            "status": stack_status,
            "issues": service_issues,
        },
        "resources": {
            "threshold_pct": int(BUDGET_THRESHOLD * 100),
            "above_threshold": pressure,
            "status": "warn" if pressure else "ok",
        },
        "work": {
            "tasks_open": len(open_tasks),
            "tasks_total": len(tasks),
            "tasks_by_priority": dict(open_by_pri),
            "open_tasks": open_tasks_detail,
            "projects_active": proj_by_status.get("active", 0) + proj_by_status.get("in_progress", 0),
            "projects_total": len(projects),
            "last_session_at": last_session_at,
            "status": work_status,
        },
        "atlas": {
            "loops": loops,
            "status": atlas_status,
        },
        "inventory": inventory,
        "syslog": syslog,
    }

    out = str(DASHBOARD_ROOT / "health-summary.json")
    return {out: json.dumps(summary, indent=2, default=str)}
