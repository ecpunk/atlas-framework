from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from schemas.project import Project
from schemas.session import Session

NAME = "session_activity_rollup"
INPUTS = ["project:*", "session:*"]
OUTPUTS = [
    "outputs/Session Activity Rollup (generated).md"
]


def _utc_hour_bucket(ts: datetime) -> str:
    hour = ts.astimezone(timezone.utc).hour
    return "off-hours" if hour < 7 or hour >= 18 else "business-hours"


def _is_recent(ts: datetime, since_hours: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return ts.astimezone(timezone.utc) >= cutoff


def _project_name_map(projects: list[Project]) -> dict[str, str]:
    return {project.id: project.name for project in projects}


def _normalize_service_token(raw: object) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    if ":" in token:
        prefix, rest = token.split(":", 1)
        if prefix in {"service", "container"} and rest.strip():
            token = rest.strip()
    return token


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


def _window_summary(sessions: list[Session], start: datetime, end: datetime) -> dict[str, object]:
    lifecycle_counts: dict[str, int] = defaultdict(int)
    hour_bucket_counts: dict[str, int] = defaultdict(int)
    project_counts: dict[str, int] = defaultdict(int)
    operator_counts: dict[int, int] = defaultdict(int)
    operator_lifecycle_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    service_counts: dict[str, int] = defaultdict(int)
    operator_service_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0

    for session in sessions:
        ts = session.timestamp.astimezone(timezone.utc)
        if ts < start or ts >= end:
            continue

        total += 1
        lifecycle_counts[session.lifecycle] += 1
        hour_bucket_counts[_utc_hour_bucket(ts)] += 1
        operator_counts[session.user_id] += 1
        operator_lifecycle_counts[session.user_id][session.lifecycle] += 1
        for project_id in session.project_ids:
            project_counts[project_id] += 1

        session_services: set[str] = set()
        for entity in session.entities_touched:
            if str(entity).strip().lower().startswith(("service:", "container:")):
                normalized = _normalize_service_token(entity)
                if normalized:
                    session_services.add(normalized)

        for call in session.tool_calls:
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
            service_counts[service_name] += 1
            operator_service_counts[session.user_id][service_name] += 1

    off_hours = hour_bucket_counts.get("off-hours", 0)
    off_hours_ratio = (off_hours / total) if total else 0.0

    return {
        "total": total,
        "off_hours": off_hours,
        "off_hours_ratio": off_hours_ratio,
        "lifecycle_counts": lifecycle_counts,
        "project_counts": project_counts,
        "operator_counts": operator_counts,
        "operator_lifecycle_counts": operator_lifecycle_counts,
        "service_counts": service_counts,
        "operator_service_counts": operator_service_counts,
    }


def generate(store: dict) -> dict[str, str]:
    projects_store = store.get("project", {})
    sessions_store = store.get("session", {})

    projects = [item for item in projects_store.values() if isinstance(item, Project)]
    sessions = [item for item in sessions_store.values() if isinstance(item, Session)]

    source_filter = "telegram-bot"
    filtered = [session for session in sessions if session.source == source_filter]
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=7)
    previous_start = now - timedelta(days=14)

    current = _window_summary(filtered, current_start, now)
    previous = _window_summary(filtered, previous_start, current_start)

    adaptive_quantile = 0.75
    adaptive_sensitivity_floor = 0.8
    adaptive_sensitivity_ceiling = 1.8
    adaptive_history_windows = 8
    adaptive_min_windows = 4

    history_totals: list[float] = []
    history_projects: dict[str, list[float]] = defaultdict(list)
    for i in range(1, adaptive_history_windows + 1):
        end = current_start - timedelta(days=7 * (i - 1))
        start = end - timedelta(days=7)
        window = _window_summary(filtered, start, end)
        history_totals.append(float(window["total"]))
        for project_id, count in window["project_counts"].items():
            history_projects[project_id].append(float(count))

    project_names = _project_name_map(projects)
    current_project_counts = current["project_counts"]
    previous_project_counts = previous["project_counts"]
    top_projects = sorted(current_project_counts.items(), key=lambda item: item[1], reverse=True)[:15]

    total_recent = int(current["total"])
    previous_total = int(previous["total"])
    total_delta = total_recent - previous_total
    total_delta_pct = (total_delta / previous_total) if previous_total else None

    off_hours = int(current["off_hours"])
    off_hours_ratio = float(current["off_hours_ratio"])
    previous_off_ratio = float(previous["off_hours_ratio"])
    off_hours_ratio_delta_pp = (off_hours_ratio - previous_off_ratio) * 100.0

    all_projects = set(current_project_counts.keys()) | set(previous_project_counts.keys())
    project_deltas: list[tuple[str, int, int, int]] = []
    for project_id in all_projects:
        cur = int(current_project_counts.get(project_id, 0))
        prev = int(previous_project_counts.get(project_id, 0))
        delta = cur - prev
        if delta == 0:
            continue
        project_deltas.append((project_id, cur, prev, delta))
    project_deltas.sort(key=lambda item: abs(item[3]), reverse=True)
    top_services = sorted(current["service_counts"].items(), key=lambda item: item[1], reverse=True)[:15]
    top_operators = sorted(current["operator_counts"].items(), key=lambda item: item[1], reverse=True)[:15]

    adaptive_profiles: list[dict[str, object]] = []
    adaptive_alerts: list[str] = []
    all_adaptive_projects = set(current_project_counts.keys()) | set(history_projects.keys())
    for project_id in sorted(all_adaptive_projects):
        current_count = int(current_project_counts.get(project_id, 0))
        history = history_projects.get(project_id, [])
        if len(history) < adaptive_min_windows:
            adaptive_profiles.append(
                {
                    "project_id": project_id,
                    "current": current_count,
                    "history_windows": len(history),
                    "threshold": None,
                    "sensitivity": None,
                }
            )
            continue

        p50 = _quantile(history, 0.5)
        p90 = _quantile(history, 0.9)
        base = _quantile(history, adaptive_quantile)
        variability = (p90 / max(p50, 1.0)) if p90 > 0 else 1.0
        sensitivity = _clamp(
            1.0 + ((variability - 1.0) * 0.5),
            adaptive_sensitivity_floor,
            adaptive_sensitivity_ceiling,
        )
        threshold = base * sensitivity

        adaptive_profiles.append(
            {
                "project_id": project_id,
                "current": current_count,
                "history_windows": len(history),
                "p50": p50,
                "p90": p90,
                "base": base,
                "variability": variability,
                "sensitivity": sensitivity,
                "threshold": threshold,
            }
        )

        if float(current_count) > threshold:
            project_name = project_names.get(project_id, project_id)
            adaptive_alerts.append(
                f"{project_name} (`{project_id}`): current {current_count} > adaptive threshold {threshold:.2f}"
            )

    volume_delta_threshold_pct = 0.5
    off_hours_delta_threshold_pp = 20.0
    operator_spike_multiplier = 2.0
    min_operator_sessions = 3

    baseline_alerts: list[str] = []
    if total_delta_pct is not None and abs(total_delta_pct) >= volume_delta_threshold_pct:
        direction = "increase" if total_delta_pct > 0 else "decrease"
        baseline_alerts.append(
            f"Session volume {direction} {total_delta_pct:+.2%} vs prior window (threshold {volume_delta_threshold_pct:.0%})."
        )

    if abs(off_hours_ratio_delta_pp) >= off_hours_delta_threshold_pp:
        direction = "increase" if off_hours_ratio_delta_pp > 0 else "decrease"
        baseline_alerts.append(
            f"Off-hours ratio {direction} {off_hours_ratio_delta_pp:+.2f} pp vs prior window "
            f"(threshold {off_hours_delta_threshold_pp:.0f} pp)."
        )

    operator_spikes: list[str] = []
    all_operator_ids = set(current["operator_counts"].keys()) | set(previous["operator_counts"].keys())
    for user_id in sorted(all_operator_ids):
        cur = int(current["operator_counts"].get(user_id, 0))
        prev = int(previous["operator_counts"].get(user_id, 0))
        if cur < min_operator_sessions:
            continue
        if prev == 0:
            if cur >= int(min_operator_sessions * operator_spike_multiplier):
                operator_spikes.append(f"user {user_id}: {cur} sessions (new concentration; prior=0)")
            continue
        ratio = cur / prev
        if ratio >= operator_spike_multiplier:
            operator_spikes.append(f"user {user_id}: {cur} vs {prev} ({ratio:.2f}x)")

    anomaly_prompts: list[str] = []
    if total_delta >= 3:
        anomaly_prompts.append(
            "Session volume increased materially week-over-week. Which projects and tools explain the rise?"
        )
    if total_delta <= -3:
        anomaly_prompts.append(
            "Session volume decreased materially week-over-week. Is work shifting to unattended automation?"
        )
    if off_hours_ratio_delta_pp >= 20:
        anomaly_prompts.append(
            "Off-hours ratio jumped by >=20 percentage points. Confirm whether this maps to incidents or planned maintenance."
        )
    if off_hours_ratio_delta_pp <= -20:
        anomaly_prompts.append(
            "Off-hours ratio dropped by >=20 percentage points. Validate that alert-driven work has normalized."
        )
    if int(current["lifecycle_counts"].get("active", 0)) >= 10 and int(current["lifecycle_counts"].get("pruned", 0)) == 0:
        anomaly_prompts.append(
            "Active sessions are accumulating with zero pruning this week. Run lifecycle hygiene (archive -> prune) review."
        )

    lines: list[str] = [
        "# Session Activity Rollup",
        "",
        "AUTO-GENERATED from atlas-store session entities. Do not hand-edit.",
        "",
        "Scope: source=telegram-bot, window=last 7 days (UTC)",
        "",
        f"Total sessions (7d): {total_recent}",
        f"Previous window total (prior 7d): {previous_total}",
        (
            f"Week-over-week delta: {total_delta:+d} ({total_delta_pct:+.2%})"
            if total_delta_pct is not None
            else f"Week-over-week delta: {total_delta:+d} (n/a; prior window was zero)"
        ),
        f"Off-hours sessions (UTC<07 or >=18): {off_hours}",
        f"Off-hours ratio: {off_hours_ratio:.2%}",
        f"Off-hours ratio delta vs prior 7d: {off_hours_ratio_delta_pp:+.2f} pp",
        "",
        "## Lifecycle Counts",
        "",
    ]

    if total_recent == 0:
        lines.extend(["(none)", ""])
    else:
        for lifecycle in ("active", "archived", "pruned"):
            lines.append(
                f"- {lifecycle}: {int(current['lifecycle_counts'].get(lifecycle, 0))} "
                f"(prior 7d: {int(previous['lifecycle_counts'].get(lifecycle, 0))})"
            )
        lines.append("")

    lines.extend(["## Top Projects by Session Touches (7d)", ""])
    if not top_projects:
        lines.extend(["(none)", ""])
    else:
        for project_id, count in top_projects:
            project_name = project_names.get(project_id, project_id)
            lines.append(f"- {project_name} (`{project_id}`): {count}")
        lines.append("")

    lines.extend(["## Top Project Delta Movers (7d vs prior 7d)", ""])
    if not project_deltas:
        lines.extend(["(none)", ""])
    else:
        for project_id, cur, prev, delta in project_deltas[:15]:
            project_name = project_names.get(project_id, project_id)
            lines.append(
                f"- {project_name} (`{project_id}`): {cur} vs {prev} ({delta:+d})"
            )
        lines.append("")

    lines.extend(["## Suggested Anomaly Triage Prompts", ""])
    if not anomaly_prompts:
        lines.extend(["- No material anomalies detected for this window.", ""])
    else:
        for prompt in anomaly_prompts:
            lines.append(f"- {prompt}")
        lines.append("")

    lines.extend(["## Per-Operator Activity (7d)", ""])
    if not top_operators:
        lines.extend(["(none)", ""])
    else:
        for user_id, count in top_operators:
            lifecycle = current["operator_lifecycle_counts"].get(user_id, {})
            lines.append(
                f"- user {user_id}: {count} sessions "
                f"(active {int(lifecycle.get('active', 0))}, "
                f"archived {int(lifecycle.get('archived', 0))}, "
                f"pruned {int(lifecycle.get('pruned', 0))})"
            )
        lines.append("")

    lines.extend(["## Service Touch Heatmap (7d)", ""])
    if not top_services:
        lines.extend(["(none)", ""])
    else:
        for service_name, count in top_services:
            lines.append(f"- {service_name}: {count}")
        lines.append("")

    lines.extend(["## Baseline Drift Alerts (7d vs prior 7d)", ""])
    lines.append(
        "Thresholds: "
        f"volume drift {volume_delta_threshold_pct:.0%}, "
        f"off-hours ratio drift {off_hours_delta_threshold_pp:.0f} pp, "
        f"operator spike {operator_spike_multiplier:.1f}x with min {min_operator_sessions} sessions."
    )
    lines.append("")
    if not baseline_alerts and not operator_spikes:
        lines.extend(["- No baseline drift alerts triggered.", ""])
    else:
        for alert in baseline_alerts:
            lines.append(f"- {alert}")
        for spike in operator_spikes[:10]:
            lines.append(f"- Operator intensity spike: {spike}")
        lines.append("")

    lines.extend(["## Adaptive Threshold Profiles", ""])
    lines.append(
        "Model: rolling 7d windows, "
        f"history windows={adaptive_history_windows}, quantile={adaptive_quantile:.2f}, "
        f"sensitivity clamp=[{adaptive_sensitivity_floor:.1f}, {adaptive_sensitivity_ceiling:.1f}]."
    )
    lines.append("")
    if not adaptive_profiles:
        lines.extend(["(none)", ""])
    else:
        ranked_profiles = sorted(
            adaptive_profiles,
            key=lambda item: int(item.get("current", 0)),
            reverse=True,
        )[:15]
        for profile in ranked_profiles:
            project_id = str(profile["project_id"])
            project_name = project_names.get(project_id, project_id)
            threshold = profile.get("threshold")
            if threshold is None:
                lines.append(
                    f"- {project_name} (`{project_id}`): current {int(profile['current'])}, "
                    f"insufficient history ({int(profile['history_windows'])} windows)"
                )
                continue
            lines.append(
                f"- {project_name} (`{project_id}`): current {int(profile['current'])}, "
                f"threshold {float(threshold):.2f}, sensitivity {float(profile['sensitivity']):.2f}, "
                f"variability {float(profile['variability']):.2f}"
            )
        lines.append("")

    lines.extend(["## Adaptive Threshold Alerts", ""])
    if not adaptive_alerts:
        lines.extend(["- No project exceeded adaptive thresholds this window.", ""])
    else:
        for alert in adaptive_alerts[:20]:
            lines.append(f"- {alert}")
        lines.append("")

    lines.extend(["## Proactive Triage Recommendations", ""])
    if not baseline_alerts and not operator_spikes:
        lines.extend(["- Maintain current monitoring cadence; no threshold crossings this window.", ""])
    else:
        lines.append("- Validate whether drift aligns with planned maintenance windows or known incident periods.")
        lines.append("- Inspect top project delta movers and service heatmap for concentrated pressure points.")
        lines.append("- If operator spikes persist for two windows, open or elevate a workload-balancing task.")
        if adaptive_alerts:
            lines.append("- For adaptive-threshold breaches, prioritize projects showing low variability and high current excess.")
        lines.append("")

    output_path = OUTPUTS[0]
    content = "\n".join(lines).rstrip() + "\n"
    return {output_path: content}
