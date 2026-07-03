from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import VocabRef, validate_iso8601_timestamp

_CANONICAL_TIERS = {"read_only", "reversible", "new_surface", "irreversible"}
# GOVERNANCE_CONVERGENCE D2 — the trust axis. Optional (additive): profiles without it
# fall through to runtime_gate.decide()'s pre-D2 backwards-compat path.
_PRINCIPAL_CLASSES = {"operator_direct", "attended_surface", "autonomous_agent"}
# D2: confirmation only means something when a human can be asked. autopilot on
# new_surface/irreversible is meaningful ONLY for operator_direct (typing IS the confirm).
_OPERATOR_ONLY_AUTOPILOT_TIERS = {"new_surface", "irreversible"}


class ExplainabilityPayload(BaseModel):
    include_fields: list[str] = Field(default_factory=list)
    default_verbosity: str = Field(..., min_length=1)
    allow_operator_expand: bool = True


class ResponseShapeOverride(BaseModel):
    tiers: list[str] = Field(default_factory=list)
    allowed_values: list[str] = Field(default_factory=list)


class TierAppealOverride(BaseModel):
    tiers: list[str] = Field(default_factory=list)
    execution_effect: str = Field(..., min_length=1)


class OverridePolicyBoundaries(BaseModel):
    allow_response_shape_override: Optional[ResponseShapeOverride] = None
    allow_tier_appeal: Optional[TierAppealOverride] = None
    forbidden_overrides: list[str] = Field(default_factory=list)


class ConsumerProfile(BaseModel):
    id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)

    input_modality: VocabRef
    auth_principal: str = Field(..., min_length=1)
    principal_class: Optional[str] = None

    allowed_action_tiers: list[VocabRef] = Field(default_factory=list)
    autopilot_eligibility: dict[str, bool] = Field(default_factory=dict)

    confirm_channel: VocabRef
    response_shape: VocabRef
    session_entity_profile: VocabRef
    tool_scope: list[str] = Field(default_factory=list)

    explainability_payload: Optional[ExplainabilityPayload] = None
    override_policy_boundaries: Optional[OverridePolicyBoundaries] = None
    notes: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_model(self) -> "ConsumerProfile":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)

        if ":" not in self.auth_principal:
            raise ValueError("auth_principal must be in scheme:value format")

        if self.principal_class is not None and self.principal_class not in _PRINCIPAL_CLASSES:
            raise ValueError(
                f"principal_class must be one of {sorted(_PRINCIPAL_CLASSES)}, got {self.principal_class!r}"
            )

        if not self.allowed_action_tiers:
            raise ValueError("allowed_action_tiers must include at least one tier")

        allowed_tier_values = {ref.value_id for ref in self.allowed_action_tiers}
        if not allowed_tier_values.issubset(_CANONICAL_TIERS):
            raise ValueError("allowed_action_tiers contains non-canonical tier values")

        missing = sorted(_CANONICAL_TIERS - set(self.autopilot_eligibility.keys()))
        if missing:
            raise ValueError(f"autopilot_eligibility missing canonical tier key(s): {', '.join(missing)}")

        for key, enabled in self.autopilot_eligibility.items():
            if key not in _CANONICAL_TIERS:
                raise ValueError(f"autopilot_eligibility has unknown tier key: {key}")
            if enabled and key not in allowed_tier_values:
                raise ValueError(f"autopilot_eligibility.{key}=true requires {key} in allowed_action_tiers")
            # D2: autopilot on new_surface/irreversible is meaningful only for operator_direct
            # (typing the command IS the confirmation). Enforced only once a class is declared.
            if (
                enabled
                and self.principal_class is not None
                and self.principal_class != "operator_direct"
                and key in _OPERATOR_ONLY_AUTOPILOT_TIERS
            ):
                raise ValueError(
                    f"autopilot_eligibility.{key}=true is invalid for principal_class="
                    f"{self.principal_class!r} (only operator_direct may autopilot {key})"
                )

        if not self.tool_scope:
            raise ValueError("tool_scope must include at least one tool entry")

        if self.confirm_channel.value_id == "none" and allowed_tier_values != {"read_only"}:
            raise ValueError("confirm_channel=none is allowed only for read_only-only profiles")

        if self.input_modality.value_id in {"natural_language", "structured_intent", "slash_command"}:
            if self.explainability_payload is None:
                raise ValueError("explainability_payload is required for conversational/intent modalities")

        return self
