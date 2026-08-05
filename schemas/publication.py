from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .conventions import TypedRef, validate_iso8601_timestamp

# Must match the profile names in
# /opt/stack/services/automations/lib/leak_gate.py (PROFILE_NAMES).
_LEAK_GATE_PROFILES = {"secrets", "base", "career", "mc-mod"}


class ReadmeFreshnessPolicy(BaseModel):
    """How aggressively the publication-drift probe checks a repo for staleness."""

    # Deterministic nudge: flag when the source area has this many commits since
    # the README was last meaningfully touched.
    max_commits_behind: int = Field(default=20, ge=0)
    # Deterministic nudge: flag when the last usage-audit is older than this.
    reaudit_after_days: int = Field(default=90, ge=0)
    # If true, the probe runs the LLM semantic "does the README still describe
    # reality?" judge on this repo (reserved for flagships). If false, only the
    # two deterministic nudges above apply.
    llm_staleness_check: bool = False
    # The costly checks (leak-gate regression, LLM staleness) run at most this
    # often, tracked by the entity's last_deep_check timestamp.
    deep_check_cadence_days: int = Field(default=7, ge=1)


class Publication(BaseModel):
    """A published repo (or doc) and its publication contract.

    Models the public surface as a first-class, drift-checkable entity: what it
    is, where its source lives, what leak-gate governs it, what it was last
    published at, and what it depends on. Read by the publication-drift probe.
    """

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)

    # Where it lives publicly.
    github_repo: str = Field(..., min_length=1)  # "owner/repo"
    remote: Optional[str] = None
    public: bool = True

    # Where it comes from. "direct" = a 1:1 source tree (source-ahead detection is
    # exact); "derived" = a transformed/subset of an upstream area (source-ahead is
    # a weaker "upstream changed since publish" nudge).
    local_source_path: Optional[str] = None
    source_kind: Literal["direct", "derived"] = "direct"

    # Governance.
    leak_gate_profile: str = "base"
    last_published_commit: Optional[str] = None
    last_usage_audit: Optional[datetime] = None
    last_deep_check: Optional[datetime] = None

    # Cross-repo dependency arrows (publication:<id>); validated as references.
    depends_on: List[TypedRef] = Field(default_factory=list)

    readme_freshness_policy: ReadmeFreshnessPolicy = Field(
        default_factory=ReadmeFreshnessPolicy
    )

    created_at: datetime
    updated_at: datetime

    @field_validator("github_repo")
    @classmethod
    def validate_repo_slug(cls, value: str) -> str:
        normalized = value.strip()
        if normalized.count("/") != 1 or normalized.startswith("/") or normalized.endswith("/"):
            raise ValueError("github_repo must be in the form 'owner/repo'")
        return normalized

    @field_validator("leak_gate_profile")
    @classmethod
    def validate_leak_gate_profile(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _LEAK_GATE_PROFILES:
            raise ValueError(
                f"leak_gate_profile must be one of {sorted(_LEAK_GATE_PROFILES)}"
            )
        return normalized

    @model_validator(mode="after")
    def validate_model(self) -> "Publication":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)
        if self.last_usage_audit is not None:
            self.last_usage_audit = validate_iso8601_timestamp(self.last_usage_audit)
        if self.last_deep_check is not None:
            self.last_deep_check = validate_iso8601_timestamp(self.last_deep_check)
        for ref in self.depends_on:
            if ref.entity_type != "publication":
                raise ValueError(
                    "depends_on entries must be publication:<id> references"
                )
        return self
