from __future__ import annotations

from schemas.consumer_profile import ConsumerProfile
from schemas.conventions import VocabRef
from schemas.vocabulary import Vocabulary

NAME = "consumer_profiles_index"
INPUTS = ["consumer_profile:*"]
OUTPUTS = [
    "outputs/Consumer Profiles Index (generated).md",
    "outputs/Consumer Profiles/<id>.md",
]

INDEX_PATH = OUTPUTS[0]
DETAIL_DIR = "outputs/Consumer Profiles"
HEADER = "AUTO-GENERATED from atlas-store/entities/consumer_profiles/*.yaml - do not hand-edit."


def _build_vocab_display_map(vocab_store: dict) -> dict[tuple[str, str], str]:
    display_map: dict[tuple[str, str], str] = {}

    for vocab in vocab_store.values():
        if isinstance(vocab, Vocabulary):
            for value in vocab.values:
                display_map[(vocab.id, value.id)] = value.name

    return display_map


def _display_vocab(ref: VocabRef, display_map: dict[tuple[str, str], str]) -> str:
    display = display_map.get((ref.vocab_id, ref.value_id))
    return display if display is not None else str(ref)


def _display_list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _format_index(profiles: list[ConsumerProfile], display_map: dict[tuple[str, str], str]) -> str:
    lines: list[str] = [
        "# Consumer Profiles",
        "",
        HEADER,
        "",
        "| ID | Display Name | Modality | Allowed Tiers | Confirm Channel | Response Shape |",
        "|----|--------------|----------|---------------|-----------------|----------------|",
    ]

    for profile in profiles:
        tiers = [ref.value_id for ref in profile.allowed_action_tiers]
        modality = _display_vocab(profile.input_modality, display_map)
        channel = _display_vocab(profile.confirm_channel, display_map)
        shape = _display_vocab(profile.response_shape, display_map)
        lines.append(
            f"| `{profile.id}` | {profile.display_name} | {modality} | {_display_list(tiers)} | {channel} | {shape} |"
        )

    return "\n".join(lines).rstrip() + "\n"


def _format_detail(profile: ConsumerProfile, display_map: dict[tuple[str, str], str]) -> str:
    lines: list[str] = []
    lines.append(f"# Consumer Profile - {profile.display_name} (`{profile.id}`)")
    lines.append("")
    lines.append(HEADER)
    lines.append("")
    lines.append(f"- **Input modality:** {_display_vocab(profile.input_modality, display_map)}")
    lines.append(f"- **Auth principal:** {profile.auth_principal}")
    lines.append(
        f"- **Allowed action tiers:** {_display_list([ref.value_id for ref in profile.allowed_action_tiers])}"
    )
    lines.append(f"- **Confirm channel:** {_display_vocab(profile.confirm_channel, display_map)}")
    lines.append(f"- **Response shape:** {_display_vocab(profile.response_shape, display_map)}")
    lines.append(
        f"- **Session entity profile:** {_display_vocab(profile.session_entity_profile, display_map)}"
    )
    lines.append("")

    lines.append("## Autopilot Eligibility")
    lines.append("")
    lines.append("| Tier | Enabled |")
    lines.append("|------|---------|")
    for tier in sorted(profile.autopilot_eligibility.keys()):
        lines.append(f"| {tier} | {str(bool(profile.autopilot_eligibility[tier])).lower()} |")
    lines.append("")

    lines.append("## Tool Scope")
    lines.append("")
    for tool in profile.tool_scope:
        lines.append(f"- {tool}")
    if not profile.tool_scope:
        lines.append("- none")
    lines.append("")

    if profile.explainability_payload is not None:
        lines.append("## Explainability Payload")
        lines.append("")
        lines.append(
            f"- **Default verbosity:** {profile.explainability_payload.default_verbosity}"
        )
        lines.append(
            f"- **Allow operator expand:** {str(profile.explainability_payload.allow_operator_expand).lower()}"
        )
        lines.append("- **Included fields:**")
        for field in profile.explainability_payload.include_fields:
            lines.append(f"  - {field}")
        lines.append("")

    if profile.override_policy_boundaries is not None:
        lines.append("## Override Policy Boundaries")
        lines.append("")
        if profile.override_policy_boundaries.allow_response_shape_override is not None:
            shape_override = profile.override_policy_boundaries.allow_response_shape_override
            lines.append(
                "- **Response shape override tiers:** "
                f"{_display_list(shape_override.tiers)}"
            )
            lines.append(
                "- **Response shape allowed values:** "
                f"{_display_list(shape_override.allowed_values)}"
            )
        if profile.override_policy_boundaries.allow_tier_appeal is not None:
            tier_appeal = profile.override_policy_boundaries.allow_tier_appeal
            lines.append(
                "- **Tier appeal tiers:** "
                f"{_display_list(tier_appeal.tiers)}"
            )
            lines.append(
                f"- **Tier appeal execution effect:** {tier_appeal.execution_effect}"
            )
        lines.append(
            "- **Forbidden overrides:** "
            f"{_display_list(profile.override_policy_boundaries.forbidden_overrides)}"
        )
        lines.append("")

    if profile.notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(profile.notes)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate(store: dict) -> dict[str, str]:
    display_map = _build_vocab_display_map(store.get("vocabulary", {}))

    profile_store = store.get("consumer_profile", {})
    profiles = [item for item in profile_store.values() if isinstance(item, ConsumerProfile)]
    profiles.sort(key=lambda item: item.id)

    outputs: dict[str, str] = {}
    outputs[INDEX_PATH] = _format_index(profiles, display_map)

    for profile in profiles:
        detail_path = f"{DETAIL_DIR}/{profile.id}.md"
        outputs[detail_path] = _format_detail(profile, display_map)

    return outputs
