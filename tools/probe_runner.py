#!/usr/bin/env python3
"""Atlas reality probes — compare canonical service entity state to actual running state.

Two probe paths based on service host:

  local  (host == the local host id):
    - deployment_path  : filesystem exists check
    - systemd_unit     : systemctl is-active
    - port             : TCP connect on 127.0.0.1
    - health_endpoint  : HTTP GET status code

  remote (host != the local host id, e.g. cloudflare-*):
    - health_endpoint  : HTTP GET status code (if set)
    - all local probes are skipped (path/systemd/port are meaningless for remote hosts)

Returns a list of per-service drift reports.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from schemas.service import maintenance_active
from tools.store import load_store

SYSTEMD_TIMEOUT = 5   # seconds
PORT_TIMEOUT = 5       # seconds
HTTP_TIMEOUT = 10      # seconds
# _probe_backup_freshness's marker lives on /mnt/nas-nfs: a plain Path.stat() on a
# hung NFS mount blocks in uninterruptible I/O with no way to interrupt it from
# this process. Every other blocking check in this file already runs through a
# subprocess with a timeout for exactly that reason (SYSTEMD_TIMEOUT/PORT_TIMEOUT/
# HTTP_TIMEOUT above) -- the marker stat was the one gap, and it is a whole-process
# gap: run_probes() loops every service sequentially, so one hung stat() here froze
# ALL 94 services' probes, not just host-backup's. Confirmed 2026-08-01 15:12-16:45:
# a NAS on the LAN went intermittently unresponsive and atlas-probe.service
# (PROBE_TIMEOUT=120s in run_atlas_probe.py) timed out 7 times in a row because this
# stat() never returned within the subprocess's own budget, leaving
# atlas_probe_latest.json frozen on its last-good snapshot for ~1h46m -- long enough
# for det.health (which only reads that file) to report an unrelated, already-passed
# blog-writer health blip as a sustained P1 the whole time the snapshot was stale.
BACKUP_STAT_TIMEOUT = 5   # seconds
# Cloudflare (and other WAFs) return a 403 bot-challenge to the default
# "Python-urllib/x.y" user-agent, which _probe_http reads as 4xx="server alive"
# — so a real 5xx on any CF-fronted service (*.workers.dev, *.pages.dev,
# a custom domain) was invisible to the health probe. A normal browser UA passes
# the UA-based bot rule and reaches the origin, so the probe sees the true
# status. (Verified 2026-08-01: urllib UA → 403, browser UA / curl → real 503.)
_PROBE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) atlas-probe/1.0"

RETIRED_LIFECYCLE_ID = "retired"
# Registered-from-birth, deliberately not built. See _apply_lifecycle().
PLANNED_LIFECYCLE_ID = "planned"
LOCAL_HOST_IDS = {os.environ.get("ATLAS_LOCAL_HOST_ID", "local")}

# Relative deployment_paths are anchored here, NOT the probe process CWD — which
# varies by caller (probe timer runs from /opt/stack/services, the MCP server and
# dashboard generator run from /opt/stack/atlas-store). Without a fixed anchor a
# relative path produces spurious "missing" drift depending on who runs the probe.
# Env var name matches mcp_server.py's _services_repo_root() (ATLAS_SERVICES_REPO_ROOT,
# same default) so both processes agree on the anchor when overridden.
STACK_SERVICES_ROOT = Path(os.environ.get("ATLAS_SERVICES_REPO_ROOT", "/opt/stack/services"))

# Services that produce a freshness marker file whose mtime advances only on a
# successful run. Lets drift surface a backup that is silently failing — the
# exists/systemd probes stay green when the timer is healthy but rsync errors out.
BACKUP_FRESHNESS = {
    "host-backup": {
        "marker": Path("/mnt/nas-nfs/Backups/host-media/latest.txt"),
        "max_age_hours": 28.0,  # daily 04:00 run + ~4h grace
    },
}


def _host_id(svc_dict: dict[str, Any]) -> str:
    """Extract the host server id from a service entity dict."""
    host = svc_dict.get("host")
    if isinstance(host, dict):
        return host.get("id", "")
    if isinstance(host, str):
        # raw YAML form: "server:example-host"
        parts = host.split(":")
        return parts[-1] if parts else ""
    return ""


def _probe_path(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = STACK_SERVICES_ROOT / p
    exists = p.exists()
    return {"type": "deployment_path", "expected": path, "actual": "exists" if exists else "missing", "pass": exists}


def _stat_mtime_guarded(path: Path, timeout: float = BACKUP_STAT_TIMEOUT) -> tuple[float | None, str | None]:
    """(mtime, error) via a subprocess-bounded stat -- never blocks this process.

    A direct `path.stat()` on a hung NFS mount blocks in uninterruptible I/O with
    no way for this process to time it out. Running `stat` as a child process
    lets subprocess.run's own timeout return control here even if the child
    itself is still stuck waiting on the mount (see BACKUP_STAT_TIMEOUT above)."""
    try:
        proc = subprocess.run(
            ["stat", "-c", "%Y", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"stat-timeout>{timeout:g}s (mount unresponsive)"
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "No such file or directory" in stderr:
            return None, "no-snapshot"
        return None, f"stat-error:{stderr or proc.returncode}"
    try:
        return float(proc.stdout.strip()), None
    except ValueError:
        return None, f"stat-unparseable:{proc.stdout.strip()!r}"


def _probe_backup_freshness(marker: Path, max_age_hours: float) -> dict[str, Any]:
    expected = f"<={max_age_hours:g}h old"
    mtime, error = _stat_mtime_guarded(marker)
    if error is not None:
        if error.startswith("stat-timeout"):
            # Mirrors aegis det.backup's reasoning (automations/aegis/detectors/
            # services.py): an unresponsive mount is a mount_down fact, not a
            # backup_stale fact -- report it as such instead of manufacturing a
            # false "stale" drift from data we simply could not read.
            return {"type": "backup_freshness", "expected": expected, "actual": error, "pass": True}
        return {"type": "backup_freshness", "expected": expected, "actual": error, "pass": False}
    age_hours = (time.time() - mtime) / 3600.0
    passed = age_hours <= max_age_hours
    return {"type": "backup_freshness", "expected": expected, "actual": f"{age_hours:.1f}h", "pass": passed}


def _probe_systemd(unit: str) -> dict[str, Any]:
    # Single call for both properties: ActiveState alone can't distinguish "unit
    # exists but is stopped" (inactive/dead, LoadState=loaded) from "unit file is
    # gone" (inactive/dead, LoadState=not-found) -- run_probes() needs that split
    # to apply expected_state semantics correctly (see _apply_expected_state).
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "LoadState,ActiveState", "--value", unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMD_TIMEOUT,
            check=False,
        )
        lines = result.stdout.strip().splitlines()
        load_state = lines[0].strip() if len(lines) > 0 and lines[0].strip() else "unknown"
        state = lines[1].strip() if len(lines) > 1 and lines[1].strip() else (result.stderr.strip() or "unknown")
    except subprocess.TimeoutExpired:
        load_state, state = "unknown", "timeout"
    except FileNotFoundError:
        load_state, state = "unknown", "systemctl-not-found"
    passed = state == "active"
    return {"type": "systemd_unit", "expected": "active", "actual": state, "pass": passed, "load_state": load_state}


def _apply_expected_state(probe: dict[str, Any], expected_state: str) -> dict[str, Any]:
    """Reinterpret a probe's pass/fail using the service's declared expected_state.

    Deliberately-parked or scheduled-oneshot services should not read as drift
    just because they are not continuously running -- that IS their healthy
    resting state. Semantics:

      allowed_stopped / retired_ignore (parked daemons, reserved-lifecycle apps):
        - systemd_unit: inactive is healthy.
        - port / container_port: closed port, or a container present-but-stopped
          ("state:exited" etc.), is healthy.
        - health_endpoint: unreachable is healthy -- a stopped container's HTTP
          endpoint being unreachable is the same underlying fact as the closed
          port, not a second independent failure.
        Choice: a FAILED unit, or a container that has vanished entirely
        (container-not-found), still reports as drift -- a crash or a deleted
        unit is not the same thing as "we stopped this on purpose", and a
        not-found unit (LoadState=not-found) still reports as drift for the
        same reason.

      on_demand (scheduled oneshots, e.g. daily governance-chain steps, AND
      spin-up-when-needed apps, e.g. seedoracle-web -- Jon: "a tool I would
      seldom use but nice to have", stopped by default, started by hand):
        - systemd_unit: inactive between scheduled runs (or between manual
          start/stop sessions) is healthy, PROVIDED the unit is still loaded
          (LoadState=loaded). A unit systemd can't find at all
          (load_state=not-found) still reports as drift, per the brief
          ("not-found unit still is [drift]").
        - port / container_port / health_endpoint: same fact as the systemd
          unit being stopped, restated three ways -- a closed port and an
          unreachable HTTP endpoint on a deliberately-stopped on_demand
          service are not a second and third independent failure. Excused
          the same as allowed_stopped/retired_ignore. Before this, an
          on_demand service that declared BOTH port and health_endpoint
          (seedoracle-web, added 2026-07-31) drifted on those two checks
          every time it sat in its correct resting state -- a false
          "degraded" on the dashboard for a service nobody asked to be up.

      unset, or any other value: unchanged (current behavior).

    No service id is special-cased here -- only the expected_state value drives
    the outcome.
    """
    if not expected_state or probe.get("pass"):
        return probe
    ptype = probe.get("type")
    actual = str(probe.get("actual", ""))

    if expected_state in ("allowed_stopped", "retired_ignore", "on_demand"):
        if ptype == "systemd_unit" and actual == "inactive" and probe.get("load_state") == "loaded":
            return {**probe, "pass": True, "note": f"expected_state={expected_state}"}
        if ptype == "port" and actual == "closed":
            return {**probe, "pass": True, "note": f"expected_state={expected_state}"}
        if ptype == "container_port" and actual.startswith("state:"):
            return {**probe, "pass": True, "note": f"expected_state={expected_state}"}
        if ptype == "health_endpoint" and actual == "unreachable":
            return {**probe, "pass": True, "note": f"expected_state={expected_state}"}

    return probe


def _apply_lifecycle(probe: dict[str, Any], lifecycle: str) -> dict[str, Any]:
    """Reinterpret a probe's pass/fail using the service's declared lifecycle.

    lifecycle=planned means the entity exists ON PAPER and is deliberately NOT
    built yet. Atlas registers planned services from birth so the units they
    will own are claimed before they exist -- otherwise the capability ledger
    reports service-desk.timer as UNREGISTERED the instant it is installed.
    The cost of that design is that every probe against a planned service is a
    probe against something nobody has written: the unit is not-found, the port
    is closed, the endpoint is unreachable. That is its CORRECT state, not
    drift, and treating it as drift is how service-desk raised a P1 immediate-
    lane page (`service_unhealthy:service:service-desk`) for the crime of being
    a plan.

    So: a planned service passes everything. Unlike expected_state=on_demand,
    this deliberately does NOT require LoadState=loaded -- a missing unit is the
    whole point. The moment the thing is built its lifecycle moves off `planned`
    and every probe becomes load-bearing again, which is the event that makes
    this safe: nothing can hide behind `planned` while running.
    """
    if lifecycle != PLANNED_LIFECYCLE_ID or probe.get("pass"):
        return probe
    return {**probe, "pass": True, "note": "lifecycle=planned"}


def _apply_maintenance(probe: dict[str, Any], svc_dict: dict[str, Any]) -> dict[str, Any]:
    """Gate 1.1: while a service's declared maintenance window is active
    (unexpired -- see schemas.service.maintenance_active for the read-time
    auto-expiry), every probe against it reports pass. The entity is EXPECTED
    to be disrupted for the declared duration, so a down unit is not drift
    and Aegis must not raise service_unhealthy for it.

    Applied broader than -- and after -- both expected_state and lifecycle:
    those describe how a service rests when nothing unusual is happening;
    maintenance is the operator overriding that for a bounded, declared window regardless
    of what lifecycle or expected_state say. The moment `until` passes, this
    stops firing on its own and normal probing resumes -- the snap-back.
    """
    if probe.get("pass") or not maintenance_active(svc_dict):
        return probe
    maintenance = svc_dict.get("maintenance") or {}
    if hasattr(maintenance, "model_dump"):
        maintenance = maintenance.model_dump()
    until = maintenance.get("until")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(until, datetime) else str(until)
    reason = (maintenance.get("reason") or "").strip() or "no reason given"
    return {**probe, "pass": True, "note": f"maintenance until {until_str}: {reason}"}


def _probe_port(port: int) -> dict[str, Any]:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PORT_TIMEOUT):
            actual = "open"
            passed = True
    except (ConnectionRefusedError, OSError, socket.timeout):
        actual = "closed"
        passed = False
    return {"type": "port", "expected": f"open:{port}", "actual": actual, "pass": passed}


def _probe_container_port(container: str, port: int) -> dict[str, Any]:
    """Verify the named container is running AND publishes the claimed host port.

    A bare TCP connect proves only that *something* listens; any port-squatting
    process (nginx, docker-proxy for another container) makes a wrong entity
    look verified. This probe ties the port claim to the actual container.
    """
    expected = f"{container} publishes :{port}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}} {{.HostConfig.NetworkMode}} {{json .NetworkSettings.Ports}}", container],
            capture_output=True, text=True, timeout=SYSTEMD_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"type": "container_port", "expected": expected,
                "actual": f"docker-unavailable:{type(exc).__name__}", "pass": False}
    if result.returncode != 0:
        return {"type": "container_port", "expected": expected,
                "actual": "container-not-found", "pass": False}

    status, netmode, ports_json = result.stdout.strip().split(" ", 2)
    host_ports: set[int] = set()
    try:
        for bindings in (json.loads(ports_json or "{}") or {}).values():
            for b in bindings or []:
                if b.get("HostPort"):
                    host_ports.add(int(b["HostPort"]))
    except (ValueError, TypeError):
        pass

    if status != "running":
        return {"type": "container_port", "expected": expected,
                "actual": f"state:{status}", "pass": False}
    if netmode == "host":
        # Host-network containers publish nothing; port liveness is covered by
        # the plain TCP probe and identity can't be inferred from bindings.
        return {"type": "container_port", "expected": expected,
                "actual": "running, network_mode=host", "pass": True}
    passed = port in host_ports
    actual = f"running, publishes {sorted(host_ports) or 'none'}"
    return {"type": "container_port", "expected": expected, "actual": actual, "pass": passed}


_UNIT_PORT_RE = re.compile(r"(?:--port[=\s]+|[A-Z_]*PORT=|127\.0\.0\.1:|0\.0\.0\.0:|localhost:)(\d{2,5})\b")


def _probe_unit_port(unit: str, port: int) -> dict[str, Any]:
    """Diff the entity's port claim against ports declared in the unit's config.

    Catches stale claims like a service silently moving ports: if the unit text declares
    ports and the claimed one is not among them, that's drift. Units that
    declare no recognizable port (e.g. port comes from an env file) pass
    informationally rather than producing false drift.
    """
    expected = f":{port} declared in {unit}"
    try:
        result = subprocess.run(
            ["systemctl", "cat", unit],
            capture_output=True, text=True, timeout=SYSTEMD_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"type": "unit_port", "expected": expected,
                "actual": f"systemctl-unavailable:{type(exc).__name__}", "pass": False}
    if result.returncode != 0:
        return {"type": "unit_port", "expected": expected,
                "actual": "unit-not-found", "pass": False}

    declared = {int(m) for m in _UNIT_PORT_RE.findall(result.stdout)}
    if not declared:
        return {"type": "unit_port", "expected": expected,
                "actual": "no-port-declared-in-unit", "pass": True}
    passed = port in declared
    return {"type": "unit_port", "expected": expected,
            "actual": f"declares {sorted(declared)}", "pass": passed}


def _probe_http(url: str) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": _PROBE_UA})
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme == "https":
            # Liveness probe only: allow self-signed/private certs to avoid false drift.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
                status = resp.status
        else:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        status = 0

    # 2xx/3xx = healthy; 4xx = server up but request was malformed (e.g. MCP streamable-http
    # rejects plain GET with 406) — treat as "alive"; 5xx or 0 = outage.
    SERVICE_DOWN_CODES = {0, 502, 503, 504}
    passed = status not in SERVICE_DOWN_CODES and status < 500
    actual = str(status) if status else "unreachable"
    return {"type": "health_endpoint", "expected": "not-5xx/unreachable", "actual": actual, "pass": passed}


def _lifecycle_id(service_dict: dict[str, Any]) -> str:
    """Extract lifecycle value_id from a service entity dict."""
    lifecycle = service_dict.get("lifecycle")
    if isinstance(lifecycle, dict):
        return lifecycle.get("value_id", "")
    if isinstance(lifecycle, str):
        # raw YAML form: "vocab:service_lifecycles:running"
        parts = lifecycle.split(":")
        return parts[-1] if parts else ""
    return ""


def run_probes(service_id: str = "") -> list[dict[str, Any]]:
    """Run probes for one service (if service_id given) or all non-retired services."""
    store = load_store(REPO_ROOT)
    services = store.get("service", {})

    if service_id:
        if service_id not in services:
            raise ValueError(f"Service '{service_id}' not found. Known ids: {sorted(services.keys())}")
        targets = {service_id: services[service_id]}
    else:
        targets = {sid: svc for sid, svc in services.items()
                   if _lifecycle_id(svc.model_dump()) != RETIRED_LIFECYCLE_ID}

    results: list[dict[str, Any]] = []

    for sid, svc in sorted(targets.items()):
        svc_dict = svc.model_dump()
        probes: list[dict[str, Any]] = []
        is_local = _host_id(svc_dict) in LOCAL_HOST_IDS
        probe_mode = "local" if is_local else "remote"

        if is_local:
            deployment_path = svc_dict.get("deployment_path")
            if deployment_path:
                probes.append(_probe_path(deployment_path))

            systemd_unit = svc_dict.get("systemd_unit")
            if systemd_unit:
                probes.append(_probe_systemd(systemd_unit))

            port = svc_dict.get("port")
            if port:
                probes.append(_probe_port(int(port)))
                container_name = svc_dict.get("container_name")
                if container_name:
                    probes.append(_probe_container_port(container_name, int(port)))
                elif systemd_unit:
                    probes.append(_probe_unit_port(systemd_unit, int(port)))

            freshness_cfg = BACKUP_FRESHNESS.get(sid)
            if freshness_cfg:
                probes.append(_probe_backup_freshness(freshness_cfg["marker"], freshness_cfg["max_age_hours"]))

        # health_endpoint is valid for both local and remote hosts
        health_endpoint = svc_dict.get("health_endpoint")
        if health_endpoint:
            if sid == "atlas-mcp":
                # Avoid self-probe deadlock: check_drift runs inside atlas-mcp.
                probes.append({
                    "type": "health_endpoint",
                    "expected": "not-5xx/unreachable",
                    "actual": "skipped-self-probe",
                    "pass": True,
                })
            else:
                probes.append(_probe_http(health_endpoint))

        expected_state = svc_dict.get("expected_state") or ""
        probes = [_apply_expected_state(p, expected_state) for p in probes]

        # Applied last, and after expected_state, because it is the broader
        # statement: expected_state describes how a BUILT service rests,
        # lifecycle=planned says there is nothing built to rest.
        lifecycle = _lifecycle_id(svc_dict)
        probes = [_apply_lifecycle(p, lifecycle) for p in probes]

        # Broadest override, applied last: a declared maintenance window excuses
        # every probe regardless of lifecycle/expected_state. See _apply_maintenance.
        probes = [_apply_maintenance(p, svc_dict) for p in probes]

        drift = any(not p["pass"] for p in probes)
        results.append({
            "service_id": sid,
            "name": svc_dict.get("name", sid),
            "lifecycle": lifecycle,
            "probe_mode": probe_mode,
            "probes": probes,
            "drift": drift,
        })

    return results


if __name__ == "__main__":
    import json
    import argparse

    parser = argparse.ArgumentParser(description="Run Atlas reality probes")
    parser.add_argument("--service", default="", help="probe a single service by id")
    args = parser.parse_args()

    try:
        results = run_probes(args.service)
        print(json.dumps(results, indent=2))
        drift_count = sum(1 for r in results if r["drift"])
        print(f"\n{len(results)} services probed. {drift_count} drifted.", file=sys.stderr)
        sys.exit(1 if drift_count else 0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
