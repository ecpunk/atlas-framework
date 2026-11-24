#!/usr/bin/env python3
"""Atlas reality probes — compare canonical service entity state to actual running state.

Two probe paths based on service host:

  local  (host == the local host id):
    - deployment_path  : filesystem exists check
    - systemd_unit     : systemctl is-active
    - port             : TCP connect on the health_endpoint's host when that
                         is a local address, else 127.0.0.1
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

from schemas.service import maintenance_active, quarantine_active
from tools.store import load_store

SYSTEMD_TIMEOUT = 5   # seconds
PORT_TIMEOUT = 5       # seconds
HTTP_TIMEOUT = 10      # seconds
# systemctl's Result= values that mean a oneshot's last run genuinely failed.
# "success" and "exec-condition" (a condition-gated skip -- ConditionPathExists
# etc. evaluated false, so the unit deliberately did not run) are NOT in this
# set -- both are healthy resting states for a oneshot, not drift.
ONESHOT_FAILURE_RESULTS = {
    "exit-code", "signal", "core-dump", "timeout", "watchdog",
    "resources", "protocol", "start-limit-hit",
}
# _probe_backup_freshness's marker may live on a network mount: a plain Path.stat() on a
# hung NFS mount blocks in uninterruptible I/O with no way to interrupt it from
# this process. Every other blocking check in this file already runs through a
# subprocess with a timeout for exactly that reason (SYSTEMD_TIMEOUT/PORT_TIMEOUT/
# HTTP_TIMEOUT above) -- the marker stat was the one gap, and it is a whole-process
# gap: run_probes() loops every service sequentially, so one hung stat() here froze
# every service's probes, not just the backup's. An unresponsive NAS makes the probe
# run time out over and over, leaving atlas_probe_latest.json frozen on its
# last-good snapshot, so anything reading that file reports stale results as current.
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
# varies by caller (probe timer runs from the services root, the MCP server and
# dashboard generator run from the engine root). Without a fixed anchor a
# relative path produces spurious "missing" drift depending on who runs the probe.
# Env var name matches mcp_server.py's _services_repo_root() (ATLAS_SERVICES_REPO_ROOT,
# same default) so both processes agree on the anchor when overridden.
STACK_SERVICES_ROOT = Path(os.environ.get("ATLAS_SERVICES_REPO_ROOT", str(Path(__file__).resolve().parent.parent)))

# Services that produce a freshness marker file whose mtime advances only on a
# successful run. Lets drift surface a backup that is silently failing — the
# exists/systemd probes stay green when the timer is healthy but rsync errors out.
# Empty by default. Shape, per service_id:
#   "nightly-backup": {"marker": Path("/srv/backups/latest.txt"), "max_age_hours": 28.0},
BACKUP_FRESHNESS: dict[str, dict[str, Any]] = {}


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
            # An unresponsive mount is a mount_down fact, not a
            # backup_stale fact -- report it as such instead of manufacturing a
            # false "stale" drift from data we simply could not read.
            return {"type": "backup_freshness", "expected": expected, "actual": error, "pass": True}
        return {"type": "backup_freshness", "expected": expected, "actual": error, "pass": False}
    age_hours = (time.time() - mtime) / 3600.0
    passed = age_hours <= max_age_hours
    return {"type": "backup_freshness", "expected": expected, "actual": f"{age_hours:.1f}h", "pass": passed}


def _probe_systemd(unit: str) -> dict[str, Any]:
    # Single call for all properties: ActiveState alone can't distinguish "unit
    # exists but is stopped" (inactive/dead, LoadState=loaded) from "unit file is
    # gone" (inactive/dead, LoadState=not-found) -- run_probes() needs that split
    # to apply expected_state semantics correctly (see _apply_expected_state).
    # Type/Result let this probe judge Type=oneshot units on their own terms
    # instead of the continuously-active model every other unit type uses (see
    # ONESHOT_FAILURE_RESULTS and the oneshot branch below).
    #
    # Deliberately NOT `--value`: `systemctl show --value` does NOT honor the
    # requested `-p` property order -- it emits systemd's own fixed internal
    # property order regardless of how they were listed on the command line
    # (verified live on this box). Positional parsing of `--value` output is a
    # landmine that silently mismatches fields the moment systemd's internal
    # order differs from the requested one. Parsing `Key=Value` lines into a
    # dict sidesteps the ordering question entirely.
    props: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "LoadState,ActiveState,SubState,Type,Result", unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMD_TIMEOUT,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
        load_state = props.get("LoadState") or "unknown"
        state = props.get("ActiveState") or (result.stderr.strip() or "unknown")
    except subprocess.TimeoutExpired:
        load_state, state = "unknown", "timeout"
    except FileNotFoundError:
        load_state, state = "unknown", "systemctl-not-found"

    sub_state = props.get("SubState", "")
    unit_type = props.get("Type", "")
    result_value = props.get("Result", "")

    if unit_type == "oneshot":
        # PASS: mid-run or just-finished (active/activating -- RemainAfterExit
        # units land here), OR resting
        # inactive with a loaded unit file and a non-failure Result (success,
        # or exec-condition -- a condition-gated skip is a deliberate no-run,
        # not a failure). Everything else -- failed, a bad Result, a
        # not-found unit, or any other ActiveState (deactivating/reloading/
        # etc) -- fails. Stay conservative on the "any other state" case.
        passed = state in ("active", "activating") or (
            state == "inactive"
            and load_state == "loaded"
            and result_value not in ONESHOT_FAILURE_RESULTS
        )
    else:
        passed = state == "active"

    return {
        "type": "systemd_unit",
        "expected": "active",
        "actual": state,
        "pass": passed,
        "load_state": load_state,
        "sub_state": sub_state,
        "unit_type": unit_type,
        "result": result_value,
    }


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
      spin-up-when-needed apps, e.g. a rarely used tool that is
      stopped by default and started by hand):
        - systemd_unit: inactive between scheduled runs (or between manual
          start/stop sessions) is healthy, PROVIDED the unit is still loaded
          (LoadState=loaded). A unit systemd can't find at all
          (load_state=not-found) still reports as drift.
        - port / container_port / health_endpoint: same fact as the systemd
          unit being stopped, restated three ways -- a closed port and an
          unreachable HTTP endpoint on a deliberately-stopped on_demand
          service are not a second and third independent failure. Excused
          the same as allowed_stopped/retired_ignore. Before this, an
          on_demand service that declared BOTH port and health_endpoint
          drifted on those two checks
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
    reports a new unit as UNREGISTERED the instant it is installed.
    The cost of that design is that every probe against a planned service is a
    probe against something nobody has written: the unit is not-found, the port
    is closed, the endpoint is unreachable. That is its CORRECT state, not
    drift, and treating it as drift pages someone for the crime of being
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


def _apply_quarantine(probe: dict[str, Any], svc_dict: dict[str, Any]) -> dict[str, Any]:
    """While a service is quarantined (set_quarantine: stopped on purpose, stays
    down until cleared) a failing probe passes with a note saying so. Before
    2026-11-11 the probe ignored quarantine, so a quarantined service read as drift
    every 15 minutes and was carded after 24 hours."""
    if probe.get("pass") or not quarantine_active(svc_dict):
        return probe
    q = svc_dict.get("quarantine") or {}
    if hasattr(q, "model_dump"):
        q = q.model_dump()
    reason = (q.get("reason") or "").strip() or "no reason given"
    return {**probe, "pass": True, "note": f"quarantined since {q.get('since')}: {reason}"}


def _apply_maintenance(probe: dict[str, Any], svc_dict: dict[str, Any]) -> dict[str, Any]:
    """While a service's declared maintenance window is active
    (unexpired -- see schemas.service.maintenance_active for the read-time
    auto-expiry), every probe against it reports pass. The entity is EXPECTED
    to be disrupted for the declared duration, so a down unit is not drift
    and the alerting system must not raise service_unhealthy for it.

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


LOOPBACK_HOST = "127.0.0.1"


def _is_local_address(addr: str) -> bool:
    """True when `addr` is an address assigned to THIS machine (loopback included).

    Binding a throwaway UDP socket to the address succeeds only for a locally
    assigned address (EADDRNOTAVAIL otherwise); nothing is sent. Wildcards are
    rejected: "0.0.0.0" is not a host a connect() can be aimed at meaningfully.
    """
    if addr in ("0.0.0.0", "::"):
        return False
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


def _port_probe_host(health_endpoint: str | None) -> str:
    """Host the local port probe connects to.

    A service published on one address only (e.g. a service bound to
    a LAN address only, no loopback bind) reads "closed" on 127.0.0.1 although
    it is up. The entity's health_endpoint names the address the service
    actually answers on, so its host part is used -- but only when that host is
    an address of this machine (an IP literal, "localhost", or a name that
    resolves to a local address). A public name (https://app.example.com via the
    tunnel) or another machine's address says nothing about where the local
    port is bound, so those, a missing/unparseable endpoint, or an endpoint with
    no host fall back to loopback, the previous behaviour.
    """
    if not health_endpoint:
        return LOOPBACK_HOST
    try:
        host = urllib.parse.urlsplit(str(health_endpoint).strip()).hostname
    except ValueError:
        return LOOPBACK_HOST
    if not host:
        return LOOPBACK_HOST
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return LOOPBACK_HOST
    for info in infos:
        addr = info[4][0]
        if _is_local_address(addr):
            return addr
    return LOOPBACK_HOST


def _probe_port(port: int, host: str = LOOPBACK_HOST) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=PORT_TIMEOUT):
            actual = "open"
            passed = True
    except (ConnectionRefusedError, OSError, socket.timeout):
        actual = "closed"
        passed = False
    # Loopback keeps the historical "open:<port>" shape; any other host is named.
    expected = f"open:{port}" if host == LOOPBACK_HOST else f"open:{host}:{port}"
    return {"type": "port", "expected": expected, "actual": actual, "pass": passed}


# Services whose declared `port` is UDP-only (game protocol), keyed by service_id.
# A plain TCP socket.create_connection() always false-positives "closed" against a
# UDP-only port: Linux only surfaces ECONNREFUSED on a UDP socket after a real
# datagram round-trip triggers an ICMP port-unreachable, which a bare TCP connect()
# never does. These are
# probed via A2S (Source/GoldSrc query protocol) instead — see _probe_a2s_port
# (python-a2s is pinned in requirements.txt). Empty by default: add
# {service_id: description} for each UDP game server the instance runs.
UDP_A2S_SERVICES: dict[str, str] = {}

# Services whose declared `port` is UDP-only AND do not speak Source/GoldSrc A2S
# either, so appending them to UDP_A2S_SERVICES would just swap one false
# failure (TCP connect on a UDP port) for another (an A2S query protocol
# mismatch). The TCP port probe is skipped outright for these; the
# already-passing container_port probe (docker inspect + published-port
# check, see _probe_container_port) carries the liveness claim instead.
UDP_NO_TCP_PROBE_SERVICES: dict[str, str] = {}


def _probe_a2s_port(host: str, port: int) -> dict[str, Any]:
    """A2S functional probe: a real protocol round-trip (query + parsed reply),
    not just a socket connect. `a2s` is imported lazily so a missing dependency
    degrades this one probe to a clear failure instead of crashing every probe."""
    try:
        import a2s
    except ImportError as exc:
        return {"type": "port", "expected": f"a2s:{port}",
                "actual": f"python-a2s not installed: {exc}", "pass": False}
    try:
        info = a2s.info((host, port), timeout=PORT_TIMEOUT)
    except Exception as exc:
        return {"type": "port", "expected": f"a2s:{port}",
                "actual": f"A2S query to {host}:{port} failed: {type(exc).__name__}: {exc}",
                "pass": False}
    return {
        "type": "port",
        "expected": f"a2s:{port}",
        "actual": f"a2s-ok: {info.map_name}, {info.player_count}/{info.max_players} players",
        "pass": True,
    }


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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Read a 3xx as itself instead of following it off the port.

    redirect_request() returning None makes urllib fall through to
    http_error_default(), which raises HTTPError(code) -- caught below the
    same way any other HTTP status is, so the 3xx status survives instead of
    being followed. Example: a site on 127.0.0.1:8080 that answers plain-HTTP
    GET / with 301 Location: https://127.0.0.1:8080/ (a force-https setting)
    while TLS terminates upstream at nginx -- following it does a TLS handshake
    against a plain-HTTP socket, raises ssl.SSLError WRONG_VERSION_NUMBER, and
    would read as status=0/"unreachable", a false service_unhealthy on every
    probe cycle.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_endpoint_problem(url: Any) -> str | None:
    """Return why a health_endpoint value cannot be probed, or None if it can.

    Shared by the probe (so a bad value is one service's failed probe, not a
    crashed run) and by the Atlas write path in mcp_server.add_service/
    update_service (so a bad value is refused when it is written). Examples of
    malformed values: prose around a URL, e.g. "http://127.0.0.1:8080/ (Host:
    ... required ...)" (urllib: "URL can't contain control characters"), and a
    bare "/healthz" (urllib: "unknown url type"). Shape only: an
    absolute http(s) URL with a host, a valid port, and no whitespace.
    """
    if not isinstance(url, str) or not url.strip():
        return "empty or not a string"
    if url != url.strip() or any(ch.isspace() for ch in url):
        return "contains whitespace (prose in the field?)"
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port  # raises ValueError on a non-numeric/out-of-range port
    except ValueError as exc:
        return f"unparseable URL: {exc}"
    if parsed.scheme.lower() not in ("http", "https"):
        return f"scheme must be http or https, got {parsed.scheme or '(none)'!r}"
    if not parsed.hostname:
        return "no host (a bare path like '/healthz' is not probeable)"
    if port == 0:
        return "port 0 is not probeable"
    return None


def _probe_http(url: str) -> dict[str, Any]:
    problem = health_endpoint_problem(url)
    if problem is not None:
        # Recorded as this service's failed probe; never raised. "malformed:" is
        # deliberately NOT "unreachable", so expected_state cannot excuse it.
        return {"type": "health_endpoint", "expected": "not-5xx/unreachable",
                "actual": f"malformed: {problem}", "pass": False}
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": _PROBE_UA})
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme == "https":
            # Liveness probe only: allow self-signed/private certs to avoid false drift.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                _NoRedirectHandler, urllib.request.HTTPSHandler(context=ctx)
            )
        else:
            opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        # ValueError: http.client.InvalidURL and friends -- anything the shape
        # check above did not anticipate still lands as "unreachable" here.
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


def run_probes(service_id: str = "", *, skipped_out: list | None = None) -> list[dict[str, Any]]:
    """Run probes for one service (if service_id given) or all non-retired services.

    A record the store could not load (``store.skipped``) is not probed. A full
    run reports each such service record as its own drifted row, probe type
    ``record_invalid``, and the rest are probed as usual. ``skipped_out``, when
    given, receives every skipped record of every kind.
    """
    store = load_store(REPO_ROOT)
    services = store.get("service", {})
    skipped = list(getattr(store, "skipped", []))
    if skipped_out is not None:
        skipped_out.extend(skipped)
    bad_services = {s["id"]: s for s in skipped if s.get("kind") == "service"}

    if service_id:
        if service_id in bad_services:
            raise ValueError(f"Service '{service_id}' has an invalid record "
                             f"({bad_services[service_id]['path']}): {bad_services[service_id]['error']}")
        if service_id not in services:
            raise ValueError(f"Service '{service_id}' not found. Known ids: {sorted(services.keys())}")
        targets = {service_id: services[service_id]}
    else:
        targets = {sid: svc for sid, svc in services.items()
                   if _lifecycle_id(svc.model_dump()) != RETIRED_LIFECYCLE_ID}

    results: list[dict[str, Any]] = []

    for sid, svc in sorted(targets.items()):
        try:
            results.append(_probe_one_service(sid, svc))
        except Exception as exc:  # noqa: BLE001 -- one bad row must not abort the run
            # Fault isolation: one service's bad entity field (or any probe bug it
            # trips) is recorded as THAT service's drift and the run continues over
            # the rest. Before this, a single malformed health_endpoint raised out
            # of run_probes(), atlas-probe.service exited non-zero every cycle, and
            # atlas_probe_latest.json froze for five days (2026-09-15..20).
            try:
                svc_dict = svc.model_dump()
            except Exception:  # noqa: BLE001
                svc_dict = {}
            results.append({
                "service_id": sid,
                "name": svc_dict.get("name", sid),
                "lifecycle": _lifecycle_id(svc_dict),
                "probe_mode": "local" if _host_id(svc_dict) in LOCAL_HOST_IDS else "remote",
                "probes": [{
                    "type": "probe_error",
                    "expected": "probe completes",
                    "actual": f"{type(exc).__name__}: {exc}",
                    "pass": False,
                }],
                "drift": True,
            })

    if not service_id:
        for sid, bad in sorted(bad_services.items()):
            results.append({
                "service_id": sid,
                "name": sid,
                "lifecycle": "",
                "probe_mode": "local",
                "probes": [{
                    "type": "record_invalid",
                    "expected": "the Atlas record loads",
                    "actual": f"{bad['path']}: {bad['error']}",
                    "pass": False,
                }],
                "drift": True,
            })

    return results


def _probe_one_service(sid: str, svc: Any) -> dict[str, Any]:
    """Probe one service entity and return its drift report (may raise)."""
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
            port_host = _port_probe_host(svc_dict.get("health_endpoint"))
            if sid in UDP_A2S_SERVICES:
                probes.append(_probe_a2s_port(port_host, int(port)))
            elif sid in UDP_NO_TCP_PROBE_SERVICES:
                # No TCP probe appended -- the container_port probe below
                # carries liveness on its own for this service.
                pass
            else:
                probes.append(_probe_port(int(port), port_host))
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
    # A quarantine is a deliberate stop that never expires on its own: a
    # quarantined service that is down is as intended, not drift.
    probes = [_apply_quarantine(p, svc_dict) for p in probes]

    drift = any(not p["pass"] for p in probes)
    return {
        "service_id": sid,
        "name": svc_dict.get("name", sid),
        "lifecycle": lifecycle,
        "probe_mode": probe_mode,
        "probes": probes,
        "drift": drift,
    }


if __name__ == "__main__":
    import json
    import argparse

    parser = argparse.ArgumentParser(description="Run Atlas reality probes")
    parser.add_argument("--service", default="", help="probe a single service by id")
    parser.add_argument("--envelope", action="store_true",
                        help='print {"results": [...], "store_skipped": [...]} instead of the bare list')
    args = parser.parse_args()

    try:
        skipped: list = []
        results = run_probes(args.service, skipped_out=skipped)
        print(json.dumps({"results": results, "store_skipped": skipped} if args.envelope else results,
                         indent=2))
        drift_count = sum(1 for r in results if r["drift"])
        print(f"\n{len(results)} services probed. {drift_count} drifted."
              + (f" {len(skipped)} invalid record(s) skipped." if skipped else ""), file=sys.stderr)
        sys.exit(1 if drift_count else 0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
