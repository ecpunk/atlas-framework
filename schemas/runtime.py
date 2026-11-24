from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import TypedRef, VocabRef, validate_iso8601_timestamp

CAB_CLASSES = {"standard", "C1", "C2", "C3", "C4", "C5"}


class RuntimeTool(BaseModel):
    """One callable of a runtime — the invoke interface a surface discovers.

    `tier` reuses vocab:action_tiers (the same vocabulary consumer-profiles use
    in allowed_action_tiers), so entitlement is just: a consumer may invoke a
    tool when its allowed_action_tiers includes the tool's tier. No separate
    entitlement field is needed.
    """

    name: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    mutates: bool  # read (runs free) vs write (gated)
    tier: VocabRef  # vocab:action_tiers:{read_only|reversible|new_surface|irreversible}
    args_schema: dict[str, Any] = Field(default_factory=dict)  # JSON-schema-ish arg spec
    # Change-approval class: "standard", or C1–C5 = the operator decides, for every
    # principal except operator_direct (see the cab-rules rule entity).
    # Argument-dependent tools also name a classifier (cab_classifier).
    cab_class: str = "standard"
    cab_classifier: Optional[str] = None

    @model_validator(mode="after")
    def validate_model(self) -> "RuntimeTool":
        if self.tier.vocab_id != "action_tiers":
            raise ValueError("tier must reference vocab:action_tiers:<value>")
        if self.cab_class not in CAB_CLASSES:
            raise ValueError(f"cab_class must be one of {sorted(CAB_CLASSES)}, got {self.cab_class!r}")
        return self


class Runtime(BaseModel):
    """A runtime: a deterministic capability defined once in Atlas, callable
    uniformly from any surface, governed once, and observable on the event plane.

    - tools: the invoke interface (discoverable; args_schema is the intent spec).
    - executor: the service that serves the tools (decoupled from callers).
    - event_subject: base multicast subject; lifecycle events publish to
      <event_subject>.<event> (e.g. runtime.media.succeeded / .failed).
    """

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    lifecycle: VocabRef  # vocab:lifecycle_categories:{current|live|blocked|defer|archive}
    tools: list[RuntimeTool] = Field(default_factory=list)
    executor: TypedRef  # service:<id> that serves the tools
    event_subject: str = Field(..., min_length=1)  # e.g. "runtime.media"
    spec_doc: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_model(self) -> "Runtime":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)
        if self.lifecycle.vocab_id != "lifecycle_categories":
            raise ValueError("lifecycle must reference vocab:lifecycle_categories:<value>")
        return self
