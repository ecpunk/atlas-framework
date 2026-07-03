#!/usr/bin/env python3
"""Atlas reality probes — compare canonical service entity state to actual running state.

Two probe paths based on service host:

  local  (host == hydra):
    - deployment_path  : filesystem exists check
    - systemd_unit     : systemctl is-active
    - port             : TCP connect on 127.0.0.1
    - health_endpoint  : HTTP GET status code

  remote (host != hydra, e.g. cloudflare-*):
    - health_endpoint  : HTTP GET status code (if set)
    - all local probes are skipped (path/systemd/port are meaningless for remote hosts)

Returns a list of per-service drift reports.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.store import load_store

SYSTEMD_TIMEOUT = 5   # seconds
PORT_TIMEOUT = 5       # seconds
HTTP_TIMEOUT = 10      # seconds

RETIRED_LIFECYCLE_ID = "retired"
LOCAL_HOST_IDS = {"hydra"}

# Relative deployment_paths are anchored here, NOT the probe process CWD — which
# varies by caller (probe timer runs from ., the MCP server and
# dashboard generator run from .). Without a fixed anchor a
# relative path produces spurious "missing" drift depending on who runs the probe.
STACK_SERVICES_ROOT = Path(".")

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
        # raw YAML form: "server:hydra"
        parts = host.split(":")
        return parts[-1] if parts else ""
    return ""


def _probe_path(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = STACK_SERVICES_ROOT / p
    exists = p.exists()
    return {"type": "deployment_path", "expected": path, "actual": "exists" if exists else "missing", "pass": exists}


def _probe_backup_freshness(marker: Path, max_age_hours: float) -> dict[str, Any]:
    expected = f"<={max_age_hours:g}h old"
    try:
        mtime = marker.stat().st_mtime
    except FileNotFoundError:
        return {"type": "backup_freshness", "expected": expected, "actual": "no-snapshot", "pass": False}
    except OSError as exc:
        return {"type": "backup_freshness", "expected": expected, "actual": f"stat-error:{exc.errno}", "pass": False}
    age_hours = (time.time() - mtime) / 3600.0
    passed = age_hours <= max_age_hours
    return {"type": "backup_freshness", "expected": expected, "actual": f"{age_hours:.1f}h", "pass": passed}


def _probe_systemd(unit: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMD_TIMEOUT,
            check=False,
        )
        state = result.stdout.strip() or result.stderr.strip() or "unknown"
    except subprocess.TimeoutExpired:
        state = "timeout"
    except FileNotFoundError:
        state = "systemctl-not-found"
    passed = state == "active"
    return {"type": "systemd_unit", "expected": "active", "actual": state, "pass": passed}


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

    Catches stale claims like service-a 8102-vs-8106: if the unit text declares
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
        req = urllib.request.Request(url, method="GET")
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

        drift = any(not p["pass"] for p in probes)
        results.append({
            "service_id": sid,
            "name": svc_dict.get("name", sid),
            "lifecycle": _lifecycle_id(svc_dict),
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
