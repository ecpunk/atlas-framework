from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import TypedRef, VocabRef, validate_iso8601_timestamp


class ServiceAuth(BaseModel):
    """Authentication configuration for a service endpoint."""

    mechanism: str  # e.g., "api_key", "oauth", "none"
    header: Optional[str] = None
    env_var: Optional[str] = None
    key_file: Optional[str] = None
    notes: Optional[str] = None


class ServiceMaintenance(BaseModel):
    """Declared maintenance window. Persona restart/alert authority
    derives from entity STATE, not a static allowlist. Setting active=true here
    is what lifts a restart lock and hushes alerts for this service; the fact
    lives on the entity, not in code that has to know the entity's id.

    `active` is validated at write time only. Whether the window is STILL in
    effect is a read-time question — see maintenance_active() below, which
    treats an expired `until` as inactive with no cleanup job required: the
    window snaps back on its own the moment `until` is in the past.
    """

    active: bool = False
    until: Optional[datetime] = None       # REQUIRED when active=True; ISO-8601 UTC
    reason: Optional[str] = None           # REQUIRED (non-empty) when active=True
    declared_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_maintenance(self) -> "ServiceMaintenance":
        if self.until is not None:
            self.until = validate_iso8601_timestamp(self.until)
        if self.declared_at is not None:
            self.declared_at = validate_iso8601_timestamp(self.declared_at)
        if self.active:
            if self.until is None:
                raise ValueError("maintenance.until is required when maintenance.active is true")
            if not (self.reason or "").strip():
                raise ValueError("maintenance.reason is required when maintenance.active is true")
        return self


class ServiceQuarantine(BaseModel):
    """A deliberate stop that every self-heal and update job must honor.

    Why: a service stopped on purpose during an incident must not be brought
    back by a self-heal job a few minutes later. While
    `active` is true, no autonomous job may start, restart, recreate or update
    this service — it beats a maintenance window and an `agent_restartable` grant
    (see agent_restart_allowed()). Unlike maintenance it never expires on its own:
    it holds until cleared. Set/clear only via Atlas set_quarantine /
    clear_quarantine; jobs read it through quarantine_active().
    """

    active: bool = False
    reason: Optional[str] = None           # REQUIRED (non-empty) when active=True
    since: Optional[datetime] = None       # REQUIRED when active=True; ISO-8601 UTC
    set_by: Optional[str] = None
    cleared_at: Optional[datetime] = None
    cleared_by: Optional[str] = None

    @model_validator(mode="after")
    def validate_quarantine(self) -> "ServiceQuarantine":
        if self.since is not None:
            self.since = validate_iso8601_timestamp(self.since)
        if self.cleared_at is not None:
            self.cleared_at = validate_iso8601_timestamp(self.cleared_at)
        if self.active:
            if self.since is None:
                raise ValueError("quarantine.since is required when quarantine.active is true")
            if not (self.reason or "").strip():
                raise ValueError("quarantine.reason is required when quarantine.active is true")
        return self


# ---------------------------------------------------------------------------
# The every-app list.
#
# Someone has to watch for software updates and vulnerabilities, so this is a
# maintained list, kept current as part of routine maintenance. Each running
# service records WHAT software it runs, where
# that software comes from, how it gets updated and, when it is held back, why,
# by whom and until when. A reconcile job can check it against the hosts and a
# vulnerability watch can read it.
# The same block hangs off a Server entity for host software that no service owns
# (the OS and third-party apt packages such as gh, Node, PHP, KiCad).
# ---------------------------------------------------------------------------

SOFTWARE_KINDS = frozenset({
    "container_image",   # pulled image (registry tag or digest)
    "local_build",       # image built here FROM an upstream base (e.g. a patched upstream image)
    "apt_package",       # Debian/Ubuntu package (Ubuntu archive or a third-party source)
    "python_runtime",    # the interpreter a host-side Python service runs on
    "python_venv",       # a virtualenv's site-packages (scanned as a directory)
    "repo_code",         # code in this repo, run in place (versioned by git, not by release)
    "binary",            # a vendor binary outside apt (game server, JRE in a tarball, ...)
    "static_site",       # files served by nginx; the server is the nginx component
    "os",                # the host operating system
})

# Update policies. What each one promises:
#   nightly              an automated update job applies it in the nightly window
#   nightly_when_idle    the same, only when the service reports no active users
#   rebuild_local        `docker build --pull` in the window; someone owns the Dockerfile
#   os_unattended        unattended-upgrades / the apt part of the window
#   pinned               held at `pin.version`; moves only by an edit, never by the window
#   manual               no automation updates it; someone named in `owner` does
#   vendor_dead_isolate  no fix will ever come: kept isolated and closed when idle
UPDATE_POLICIES = frozenset({"nightly", "nightly_when_idle", "rebuild_local", "os_unattended",
                             "pinned", "manual", "vendor_dead_isolate"})
_IMAGE_KINDS = frozenset({"container_image", "local_build"})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SoftwarePin(BaseModel):
    """Why a component is held back. All four fields are required: a pin with no
    owner or review date is how a version becomes permanent by accident."""

    version: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    review_by: str                      # ISO date (YYYY-MM-DD)

    @model_validator(mode="after")
    def validate_pin(self) -> "SoftwarePin":
        if not self.reason.strip() or not self.owner.strip():
            raise ValueError("pin.reason and pin.owner must be non-empty")
        if not _ISO_DATE.match(self.review_by or ""):
            raise ValueError("pin.review_by must be an ISO date YYYY-MM-DD")
        date.fromisoformat(self.review_by)
        return self


class SoftwareComponent(BaseModel):
    """One piece of software a service (or host) runs."""

    name: str = Field(..., min_length=1)
    kind: str
    source: str = Field(..., min_length=1)   # upstream: image repo, project URL, apt source, "this repo"
    update_policy: str
    running_version: Optional[str] = None
    image: Optional[str] = None              # image ref as compose/docker runs it
    digest: Optional[str] = None             # image id (sha256:...) of the running container
    container: Optional[str] = None
    host: Optional[str] = None               # server id when it is not the service's own host
    base_image: Optional[str] = None         # local_build: the FROM it rebuilds on
    build_context: Optional[str] = None
    package: Optional[str] = None            # apt_package: the package name
    apt_source: Optional[str] = None         # apt_package: the sources.list.d file (or "ubuntu")
    path: Optional[str] = None               # binary / venv / runtime location on the host
    latest_version: Optional[str] = None
    latest_date: Optional[str] = None
    checked_at: Optional[datetime] = None
    pin: Optional[SoftwarePin] = None
    data_paths: list[str] = Field(default_factory=list)   # what to snapshot before an update
    health_check: Optional[str] = None
    advisory_feed: Optional[str] = None      # vendor advisory source the vuln watch reads
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_component(self) -> "SoftwareComponent":
        if self.kind not in SOFTWARE_KINDS:
            raise ValueError(f"software kind must be one of {sorted(SOFTWARE_KINDS)}")
        if self.update_policy not in UPDATE_POLICIES:
            raise ValueError(f"update_policy must be one of {sorted(UPDATE_POLICIES)}")
        if self.update_policy == "pinned" and self.pin is None:
            raise ValueError(f"{self.name}: update_policy pinned requires a pin (version, reason, owner, review_by)")
        if self.kind in _IMAGE_KINDS and not self.image:
            raise ValueError(f"{self.name}: {self.kind} requires image")
        if self.kind == "local_build" and not self.base_image:
            raise ValueError(f"{self.name}: local_build requires base_image")
        if self.kind == "apt_package" and not self.package:
            raise ValueError(f"{self.name}: apt_package requires package")
        if self.checked_at is not None:
            self.checked_at = validate_iso8601_timestamp(self.checked_at)
        return self


class ServiceSoftware(BaseModel):
    """The `software` block: every component plus when it was last recorded."""

    components: list[SoftwareComponent] = Field(..., min_length=1)
    recorded_at: datetime
    recorded_by: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_software(self) -> "ServiceSoftware":
        self.recorded_at = validate_iso8601_timestamp(self.recorded_at)
        names = [c.name for c in self.components]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"software component names must be unique: {dupes}")
        return self


# ---------------------------------------------------------------------------
# Internet exposure.
#
# Who may reach a service from the internet and through which door. A door
# check can read every opening (router port forwards, tunnel routes, DNS at the
# public IP, nginx vhosts on public ports, Access apps) and treat anything not
# claimed here as an opening to close. set_exposure is the way to add one. A service may have several doors (e.g. a public site through the
# 443 forward, plus a family-only tunnel name), so this is a list.
#
#   who  none     not reachable from the internet
#        operator the operator only
#        family   named people (Access app: named groups)
#        public   anyone; the app's own login, if any, is the only check
#   how  tunnel+access  Cloudflare tunnel route behind a Cloudflare Access app
#        tunnel         tunnel route, no Access app
#        forward        a router port forward to the public IP (origin :443 etc.)
# ---------------------------------------------------------------------------

EXPOSURE_WHO = frozenset({"none", "operator", "family", "public"})
EXPOSURE_HOW = frozenset({"tunnel+access", "tunnel", "forward"})


class ServiceExposure(BaseModel):
    """One door into a service from the internet."""

    who: str
    how: Optional[str] = None             # REQUIRED unless who == none
    reason: str = Field(..., min_length=1)
    hostnames: list[str] = Field(default_factory=list)   # tunnel routes / vhost names this door covers
    forwards: list[str] = Field(default_factory=list)    # external ports of router forwards ("443")
    source_ips: list[str] = Field(default_factory=list)  # forward limited to these source IPs
    until: Optional[str] = None           # ISO date the door closes (required with source_ips)
    recorded_at: Optional[datetime] = None
    recorded_by: Optional[str] = None

    @model_validator(mode="after")
    def validate_exposure(self) -> "ServiceExposure":
        if self.who not in EXPOSURE_WHO:
            raise ValueError(f"exposure.who must be one of {sorted(EXPOSURE_WHO)}")
        if not self.reason.strip():
            raise ValueError("exposure.reason must be non-empty")
        self.hostnames = [h.strip().lower() for h in self.hostnames if h.strip()]
        if self.until is not None:
            if not _ISO_DATE.match(self.until):
                raise ValueError("exposure.until must be an ISO date YYYY-MM-DD")
            date.fromisoformat(self.until)
        if self.recorded_at is not None:
            self.recorded_at = validate_iso8601_timestamp(self.recorded_at)
        if self.who == "none":
            if self.how or self.hostnames or self.forwards or self.source_ips:
                raise ValueError("exposure who=none takes no how, hostnames, forwards or source_ips")
            return self
        if self.how not in EXPOSURE_HOW:
            raise ValueError(f"exposure.how must be one of {sorted(EXPOSURE_HOW)} when who is {self.who}")
        if not self.hostnames and not self.forwards:
            raise ValueError("exposure needs at least one hostname or forward")
        if self.how == "tunnel" and self.who != "public":
            raise ValueError("exposure how=tunnel (no Access app) means who=public; operator/family need tunnel+access")
        if self.how == "tunnel+access":
            if self.who not in {"operator", "family"}:
                raise ValueError("exposure how=tunnel+access is for who=operator or family")
            if self.forwards:
                raise ValueError("exposure how=tunnel+access takes no forwards")
        if self.how == "forward":
            if not self.forwards:
                raise ValueError("exposure how=forward needs forwards (external ports)")
            if self.who != "public" and not self.source_ips:
                raise ValueError("an operator/family forward must be limited to source_ips")
        if self.source_ips and not self.until:
            raise ValueError("exposure source_ips need an until date")
        return self


# ---------------------------------------------------------------------------
# Declared public API routes.
#
# The routes of an internet-exposed service that may answer anyone with no
# credentials. automations/netsec/api_exposure.py calls every API route of every
# service with a door from the internet, daily and with no credentials, and the
# service desk tickets any 2xx from a route not listed here. Written only through
# the Atlas set_public_api tool.
#
#   routes        "METHOD /path/template" exactly as the app's OpenAPI names it
#   route_source  where the route list comes from (an OpenAPI JSON URL); empty =
#                 https://<hostname>/openapi.json, then http://127.0.0.1:<port>/openapi.json
#   api_prefixes  only paths under these prefixes are API routes (pages are not);
#                 empty = ["/api/"] when the app has any, else every path
# ---------------------------------------------------------------------------

_ROUTE_RE = r"^(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) /\S*$"


class ServicePublicApi(BaseModel):
    """The no-credential API routes an internet-exposed service declares."""

    routes: list[str] = Field(default_factory=list)
    route_source: Optional[str] = None
    api_prefixes: list[str] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)
    recorded_at: Optional[datetime] = None
    recorded_by: Optional[str] = None

    @model_validator(mode="after")
    def validate_public_api(self) -> "ServicePublicApi":
        import re
        bad = [r for r in self.routes if not re.match(_ROUTE_RE, r)]
        if bad:
            raise ValueError(f"public_api.routes must look like 'GET /api/x': {bad}")
        if len(set(self.routes)) != len(self.routes):
            raise ValueError("public_api.routes must be unique")
        if any(not p.startswith("/") for p in self.api_prefixes):
            raise ValueError("public_api.api_prefixes must start with /")
        return self


class ServiceParkedUnit(BaseModel):
    """A member kept but not meant to run: stopped on purpose, files and data
    kept, waiting to be deleted.

    Why not owns_units: many readers take owns_units as "should run" (restart
    authority, probes, the network baseline, config classes). A parked member
    listed there would be restarted, probed and alerted on. Listed here, it
    still has a recorded home, so the lookup and the capability ledger do not
    report it as unregistered, and the lookup calls it running_unexpected if
    it comes back up.
    """

    name: str = Field(..., min_length=1)   # container name, or a unit name with its suffix
    reason: str = Field(..., min_length=1)
    since: str                             # ISO date YYYY-MM-DD it was parked
    delete_after: Optional[str] = None     # ISO date YYYY-MM-DD; a parked thing should end

    @model_validator(mode="before")
    @classmethod
    def _dates_as_text(cls, data: Any) -> Any:
        # An unquoted YAML date loads as a date object; keep the ISO text.
        if isinstance(data, dict):
            data = dict(data)
            for key in ("since", "delete_after"):
                if isinstance(data.get(key), date):
                    data[key] = data[key].isoformat()
        return data

    @model_validator(mode="after")
    def validate_parked(self) -> "ServiceParkedUnit":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("parked_units.name must be non-empty")
        if not self.reason.strip():
            raise ValueError("parked_units.reason must be non-empty")
        for label, value in (("since", self.since), ("delete_after", self.delete_after)):
            if value is None:
                continue
            if not _ISO_DATE.match(str(value)):
                raise ValueError(f"parked_units.{label} must be an ISO date YYYY-MM-DD")
            date.fromisoformat(str(value))
        if self.delete_after is not None and self.delete_after < self.since:
            raise ValueError("parked_units.delete_after must not be before since")
        return self


class ServiceAlertMute(BaseModel):
    """One alert this service has silenced, with why and since when. The mute
    sits in the record of the service it silences, for the alerting system to
    read as an alert mute; matched like any alert mute: ``identity`` exact or
    ``pattern`` (fnmatch) against the alert identity, off after ``expires``.
    """

    identity: Optional[str] = None
    pattern: Optional[str] = None
    reason: str = Field(..., min_length=1)
    date: str                              # ISO date YYYY-MM-DD the mute was set
    expires: Optional[str] = None          # ISO date YYYY-MM-DD; past it the mute stops matching

    @model_validator(mode="before")
    @classmethod
    def _dates_as_text(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for key in ("date", "expires"):
                if isinstance(data.get(key), date):
                    data[key] = data[key].isoformat()
        return data

    @model_validator(mode="after")
    def validate_mute(self) -> "ServiceAlertMute":
        if not (self.identity or self.pattern):
            raise ValueError("alert_mutes entry needs an identity or a pattern")
        if not self.reason.strip():
            raise ValueError("alert_mutes.reason must be non-empty")
        for label, value in (("date", self.date), ("expires", self.expires)):
            if value is None:
                continue
            if not _ISO_DATE.match(str(value)):
                raise ValueError(f"alert_mutes.{label} must be an ISO date YYYY-MM-DD")
            date.fromisoformat(str(value))
        return self


class Service(BaseModel):
    """Service entity model for Atlas runtime service metadata."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    service_type: VocabRef
    lifecycle: VocabRef

    host: TypedRef
    deployment_path: str = Field(..., min_length=1)
    port: Optional[int] = None
    health_endpoint: Optional[str] = None
    systemd_unit: Optional[str] = None
    network_binding: Optional[str] = None  # "localhost", "lan", "tailscale", "public"
    auth: Optional[ServiceAuth] = None
    remote: Optional[str] = None

    # Restart lock / alert hush is read from THIS block,
    # not from a static allowlist. See ServiceMaintenance and maintenance_active().
    maintenance: Optional[ServiceMaintenance] = None

    # A deliberate stop. Beats maintenance and
    # agent_restartable; never auto-expires. See ServiceQuarantine and quarantine_active().
    quarantine: Optional[ServiceQuarantine] = None

    # The every-app list. See ServiceSoftware.
    software: Optional[ServiceSoftware] = None

    # Who may reach it from the internet, and how.
    # See ServiceExposure. Absent = not declared; the door check treats any opening
    # to an undeclared service as not allowed.
    exposure: Optional[list[ServiceExposure]] = None

    # The API routes that may answer anyone. See ServicePublicApi.
    public_api: Optional[ServicePublicApi] = None

    # Every systemd unit and container this service owns — the authoritative
    # binding between an entity and reality, superseding the systemd_unit and
    # container_name scalars above. systemd splits one logical service into
    # several units (e.g. app.service plus app-digest/-metrics timers), and before
    # this was declared it was inferred from name prefixes, which quietly
    # absorbed units into parents that were themselves already retired.
    # The capability ledger reports anything on the box that no entity claims
    # here, so an omission surfaces as UNREGISTERED instead of as silence.
    owns_units: list[str] = Field(default_factory=list)

    # Members kept but not meant to run (stopped on purpose, awaiting deletion).
    # Never also in owns_units. See ServiceParkedUnit.
    parked_units: list[ServiceParkedUnit] = Field(default_factory=list)

    # Alerts this service has silenced (alert mutes). See ServiceAlertMute.
    alert_mutes: list[ServiceAlertMute] = Field(default_factory=list)

    # Set when lifecycle is retired. A bare lifecycle flip records that
    # something is gone but not why, which is how the same thing gets rebuilt
    # six months later.
    retired_at: Optional[datetime] = None
    retired_reason: Optional[str] = None
    superseded_by: Optional[str] = None

    # ISO date by which a temporary thing must be renewed or removed. Registered
    # temporary things are fine; undated ones become permanent by default.
    review_by: Optional[str] = None

    owned_by: TypedRef
    depends_on: list[TypedRef] = Field(default_factory=list)

    # Docker-specific operational metadata (applies to docker_container service_type)
    container_name: Optional[str] = None   # docker container name for health / restart ops
    restartable: Optional[bool] = None     # TECHNICAL property only: can this container/unit
                                            # tolerate a restart at all. Pre-existing on 66
                                            # entities (incl. many long-running services)
                                            # and NOT an authority signal -- an autonomous persona
                                            # must never read this field to decide whether it may
                                            # act. See agent_restartable below.
    agent_restartable: Optional[bool] = None  # AUTHORITY grant: true means
                                            # an autonomous persona may restart/reset this
                                            # service's units. Absent/None => locked. Sparse by
                                            # design -- set only on entities the operator
                                            # approves, distinct from and independent of
                                            # `restartable` above. See agent_restart_allowed().
    tier: Optional[str] = None            # operational tier: "critical", "standard", "optional"
    expected_state: Optional[str] = None  # "running_required", "allowed_stopped", "on_demand", "retired_ignore"
    # Why it is meant to be in that state, who said so
    # and since when. "Stopped on purpose" with no reason is what a session cannot
    # tell from "stopped and nobody noticed"; update_service refuses a change of
    # expected_state without a reason.
    expected_state_reason: Optional[str] = None
    expected_state_set_by: Optional[str] = None
    expected_state_since: Optional[str] = None   # ISO timestamp
    # Other names people and tools use for this service ("db", "HA", "hass").
    # Name lookups resolve these.
    aliases: list[str] = Field(default_factory=list)
    protected_high_use: Optional[bool] = None  # exclude from generic high-memory restart actions
    backup_unit: Optional[str] = None     # systemd unit name for backup job (e.g. "app-backup.service")

    last_health_check: Optional[datetime] = None
    last_health_status: Optional[VocabRef] = None

    failure_modes: list[str] = Field(default_factory=list)
    resource_budget_ram_mb: Optional[int] = Field(default=None, ge=0)

    # Publication disposition — whether/how this service's code reaches GitHub.
    # "in lifecycle" (modeled/governed) is a separate axis from "published".
    #   private     — homelab-specific, never published (the safe default decision)
    #   fold        — its pattern folds into an existing published repo (publication_target)
    #   standalone  — genuinely reusable; carve out to its own repo (publication_target)
    #   cookbook    — small reusable pattern for the curated homelab cookbook repo
    #   undecided   — not yet triaged (schema default; a nudge, never implies public)
    publication_disposition: str = "undecided"
    publication_target: Optional[str] = None  # repo name for fold / standalone

    # Portal navigation fields
    external_url: Optional[str] = None     # canonical public URL (HTTPS) for this service
    portal_url: Optional[str] = None       # URL to use inside portal iframe (overrides external_url)
    portal_path: Optional[str] = None      # relative portal sub-path (e.g. /s/app via a portal domain)
    portal_label: Optional[str] = None     # display name override in portal nav (defaults to name)
    portal_visible: Optional[bool] = None  # true = include in generated portal nav
    auth_gated: Optional[bool] = None      # true = nginx vhost requires authgate.conf login
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamps(self) -> "Service":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)

        if self.last_health_check is not None:
            self.last_health_check = validate_iso8601_timestamp(self.last_health_check)

        allowed = {"private", "fold", "standalone", "cookbook", "undecided"}
        if self.publication_disposition not in allowed:
            raise ValueError(
                f"publication_disposition must be one of {sorted(allowed)}"
            )
        if self.publication_disposition in {"fold", "standalone"} and not self.publication_target:
            raise ValueError(
                "publication_target is required when publication_disposition is fold or standalone"
            )

        allowed_expected_states = {"running_required", "allowed_stopped", "on_demand", "retired_ignore"}
        if self.expected_state is not None and self.expected_state not in allowed_expected_states:
            raise ValueError(
                f"expected_state must be one of {sorted(allowed_expected_states)} or None"
            )
        if self.expected_state_since is not None:
            if _coerce_utc(self.expected_state_since) is None:
                raise ValueError("expected_state_since must be an ISO timestamp")
        cleaned = [str(a).strip() for a in self.aliases]
        if any(not a for a in cleaned):
            raise ValueError("aliases must be non-empty strings")
        if len({a.lower() for a in cleaned}) != len(cleaned):
            raise ValueError("aliases must not repeat")
        self.aliases = cleaned

        # Tier "drill" marks a throwaway container whose alerts the alerting
        # system routes to a test channel instead of a real one (and can apply
        # the same rule again at read time, against the clock). A drill must be dated and
        # short-lived, so a leftover record runs out on its own.
        if (self.tier or "").strip().lower() == "drill":
            if not self.review_by or not _ISO_DATE.match(str(self.review_by)):
                raise ValueError("a drill (tier: drill) needs review_by as an ISO date YYYY-MM-DD")
            review = date.fromisoformat(str(self.review_by))
            made = _coerce_utc(self.created_at)
            if made is not None and not 0 <= (review - made.date()).days <= 2:
                raise ValueError("a drill's review_by must be on or within 2 days after created_at")

        parked = [p.name.lower() for p in self.parked_units]
        if len(set(parked)) != len(parked):
            raise ValueError("parked_units must not repeat a name")
        owned = {str(u).strip().lower().removeprefix("docker:") for u in self.owns_units}
        both = sorted(set(parked) & owned)
        if both:
            raise ValueError(f"parked_units and owns_units must not share a name: {both}")

        return self


def _coerce_utc(value: Any) -> Optional[datetime]:
    """Parse a maintenance timestamp from either form a caller might hold:
    a raw YAML dict (string, possibly trailing 'Z') or a model_dump()'d dict
    (already a datetime). Naive datetimes are assumed UTC, matching the rest
    of this store's ISO-8601-UTC convention."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def maintenance_active(service_dict: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """The single read-time implementation of maintenance auto-expiry.

    `active: true` with `until` in the past reads as INACTIVE here — no cleanup
    job ever has to walk the store flipping the flag back; the window snaps
    back on its own at the next read past `until`. Any caller that needs to
    know "is this service currently excused" (probe_runner, restart-lock
    checks, alert routing) must call this rather than reading
    maintenance.active off the entity directly, or it will honor an expired
    window forever.

    Accepts a service dict in either shape: raw YAML (maintenance.until is a
    string) or model_dump()'d (maintenance.until is a datetime, or a nested
    ServiceMaintenance/BaseModel instance under raw-attribute access).
    """
    if not isinstance(service_dict, dict):
        return False
    maintenance = service_dict.get("maintenance")
    if maintenance is None:
        return False
    if isinstance(maintenance, BaseModel):
        maintenance = maintenance.model_dump()
    if not isinstance(maintenance, dict) or not maintenance.get("active"):
        return False

    until = _coerce_utc(maintenance.get("until"))
    if until is None:
        return False

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return until > now


def quarantine_active(service_dict: dict[str, Any]) -> bool:
    """True while the service is quarantined (a deliberate stop). No expiry: it
    holds until clear_quarantine. Same rule as automations/lib/quarantine.py's
    block_active(), which the jobs use without pydantic. Accepts raw YAML or
    model_dump()'d dicts, or a nested ServiceQuarantine instance."""
    if not isinstance(service_dict, dict):
        return False
    q = service_dict.get("quarantine")
    if q is None:
        return False
    if isinstance(q, BaseModel):
        q = q.model_dump()
    return isinstance(q, dict) and bool(q.get("active"))


def agent_restart_allowed(service_dict: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """The single shared derivation for whether an autonomous persona may
    restart/reset this service's units right now.

    Deliberately NOT derived from `restartable` -- that field is a pre-existing
    TECHNICAL property ("this unit's process model tolerates a restart") set on
    many entities, including most long-running services, and reading it
    as authority over-grants exactly those entities the operator must keep locked from
    autonomous action. Authority is instead a separate, sparse, explicit grant:
    `agent_restartable: true`, set only on entities the operator approves.
    Absent (None) or False means locked, full stop.

    Always False while quarantine_active(service_dict) -- a deliberate stop
    beats both unlocks below.

    Otherwise True when EITHER:
      - maintenance_active(service_dict, now) is True -- the existing
        auto-expiring unlock, unchanged and still the highest-precedence
        excuse (a declared maintenance window overrides everything except a
        quarantine); or
      - `agent_restartable` is True AND lifecycle is not `retired` or
        `planned` -- a grant left on a retired or never-built entity is stale
        or inert, not live authority, so it does not count.

    Accepts a service dict in either shape: raw YAML (lifecycle is a
    "vocab:service_lifecycles:x" string) or model_dump()'d (lifecycle is a
    dict with value_id, or a nested VocabRef/BaseModel instance).
    """
    # A quarantine is a deliberate stop and beats every
    # unlock below, maintenance included.
    if quarantine_active(service_dict):
        return False
    if maintenance_active(service_dict, now):
        return True
    if not isinstance(service_dict, dict):
        return False
    if service_dict.get("agent_restartable") is not True:
        return False

    lifecycle = service_dict.get("lifecycle")
    if isinstance(lifecycle, BaseModel):
        lifecycle = lifecycle.model_dump()
    if isinstance(lifecycle, dict):
        lifecycle_id = lifecycle.get("value_id", "")
    elif isinstance(lifecycle, str):
        # raw YAML form: "vocab:service_lifecycles:running"
        parts = lifecycle.split(":")
        lifecycle_id = parts[-1] if parts else ""
    else:
        lifecycle_id = ""

    return lifecycle_id not in {"retired", "planned"}