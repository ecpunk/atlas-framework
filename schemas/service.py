from __future__ import annotations

from datetime import datetime, timezone
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
    """Declared maintenance window — Gate 1.1: persona restart/alert authority
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

    # Gate 1.1 (2026-07-27): restart lock / alert hush is read from THIS block,
    # not from a static allowlist. See ServiceMaintenance and maintenance_active().
    maintenance: Optional[ServiceMaintenance] = None

    # Every systemd unit and container this service owns — the authoritative
    # binding between an entity and reality, superseding the systemd_unit and
    # container_name scalars above. systemd splits one logical service into
    # several units (aegis owns aegis/-digest/-metrics/-deadman), and before
    # this was declared it was inferred from name prefixes, which quietly
    # absorbed units into parents that were themselves already retired.
    # The capability ledger reports anything on the box that no entity claims
    # here, so an omission surfaces as UNREGISTERED instead of as silence.
    owns_units: list[str] = Field(default_factory=list)

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
                                            # entities (incl. every Minecraft/VEIN/media service)
                                            # and NOT an authority signal -- an autonomous persona
                                            # must never read this field to decide whether it may
                                            # act. See agent_restartable below.
    agent_restartable: Optional[bool] = None  # AUTHORITY grant (Gate 1.1, 2026-07-27): true means
                                            # an autonomous persona may restart/reset this
                                            # service's units. Absent/None => locked. Sparse by
                                            # design -- seeded on exactly the ten entities Jon
                                            # approved, distinct from and independent of
                                            # `restartable` above. See agent_restart_allowed().
    tier: Optional[str] = None            # operational tier: "critical", "standard", "optional"
    expected_state: Optional[str] = None  # "running_required", "allowed_stopped", "on_demand", "retired_ignore"
    protected_high_use: Optional[bool] = None  # exclude from generic high-memory restart actions
    backup_unit: Optional[str] = None     # systemd unit name for backup job (e.g. "minecraft-backup-vanilla.service")

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
    """The single read-time implementation of Gate 1.1 auto-expiry.

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


def agent_restart_allowed(service_dict: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """The single shared derivation for whether an autonomous persona may
    restart/reset this service's units right now (Gate 1.1 resolution,
    2026-07-27).

    Deliberately NOT derived from `restartable` -- that field is a pre-existing
    TECHNICAL property ("this unit's process model tolerates a restart") set on
    66 entities, including every Minecraft/VEIN/media service, and reading it
    as authority over-grants exactly those entities the operator must keep locked from
    autonomous action. Authority is instead a separate, sparse, explicit grant:
    `agent_restartable: true`, seeded on exactly the ten entities approved at
    Gate 1.1. Absent (None) or False means locked, full stop.

    True when EITHER:
      - maintenance_active(service_dict, now) is True -- the existing
        auto-expiring unlock, unchanged and still the highest-precedence
        excuse (a declared maintenance window overrides everything else); or
      - `agent_restartable` is True AND lifecycle is not `retired` or
        `planned` -- a grant left on a retired or never-built entity is stale
        or inert, not live authority, so it does not count.

    Accepts a service dict in either shape: raw YAML (lifecycle is a
    "vocab:service_lifecycles:x" string) or model_dump()'d (lifecycle is a
    dict with value_id, or a nested VocabRef/BaseModel instance).
    """
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