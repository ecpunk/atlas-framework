"""Shared reference resolution — used by validate.py and mcp_server.py.

A VocabRef/TypedRef validates only its *shape* (schemas/conventions.py): "vocab:x:y"
and "type:id" respectively. Neither checks that the thing referenced exists. This
module supplies the membership check, so the write path and the validator agree on
what a resolvable reference is.

The error/warning split is deliberate and matches validate.py's long-standing
semantics:

  VocabRef miss  -> ERROR.   A value_id that is not in its vocabulary is corruption;
                             nothing downstream can resolve it, and rules_doc's
                             _resolve_vocab_value raises outright on one.
  TypedRef miss  -> WARNING. Dangling entity refs are routine and tolerated — e.g.
                             ~29 services are owned_by a project that was never
                             modelled as an entity. Promoting these to errors would
                             block writes across a third of the catalog.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from schemas.conventions import TypedRef, VocabRef
from schemas.vocabulary import Vocabulary


def iter_refs(obj: Any) -> tuple[list[TypedRef], list[VocabRef]]:
    """Walk a model/dict/sequence and collect every TypedRef and VocabRef within."""
    typed: list[TypedRef] = []
    vocab: list[VocabRef] = []

    def walk(value: Any) -> None:
        if isinstance(value, TypedRef):
            typed.append(value)
            return
        if isinstance(value, VocabRef):
            vocab.append(value)
            return
        if isinstance(value, BaseModel):
            for field_value in value.__dict__.values():
                walk(field_value)
            return
        if isinstance(value, dict):
            for field_value in value.values():
                walk(field_value)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    walk(obj)
    return typed, vocab


def build_ref_index(store: dict[str, dict[str, BaseModel]]) -> tuple[set[str], dict[str, set[str]]]:
    """From a loaded Store, build the two lookup sets ref-checking needs.

    Returns (entity_refs, vocab_values):
      entity_refs  {"project:atlas", "service:aegis", ...}   — f"{kind}:{id}"
      vocab_values {"service_types": {"mcp_http", ...}, ...}
    """
    entity_refs: set[str] = set()
    vocab_values: dict[str, set[str]] = {}

    for kind, entities in store.items():
        if kind == "vocabulary":
            for vocab_id, model in entities.items():
                if isinstance(model, Vocabulary):
                    vocab_values[vocab_id] = {item.id for item in model.values}
            continue
        for entity_id in entities:
            entity_refs.add(f"{kind}:{entity_id}")

    return entity_refs, vocab_values


def check_refs(
    model: Any,
    entity_refs: set[str],
    vocab_values: dict[str, set[str]],
    *,
    self_ref: str | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve one model's references. Returns (errors, warnings).

    self_ref: the "kind:id" of the entity being written. Added to entity_refs so a
    new self-referential entity does not warn against a store snapshot taken before
    it existed.
    """
    known_entities = entity_refs | {self_ref} if self_ref else entity_refs

    errors: list[str] = []
    warnings: list[str] = []

    typed_refs, vocab_refs = iter_refs(model)

    for vocab_ref in vocab_refs:
        values = vocab_values.get(vocab_ref.vocab_id)
        if values is None:
            errors.append(
                f"unknown vocabulary '{vocab_ref.vocab_id}' in reference {vocab_ref}"
            )
        elif vocab_ref.value_id not in values:
            errors.append(
                f"unresolved vocabulary reference {vocab_ref} — "
                f"'{vocab_ref.vocab_id}' legal values: {sorted(values)}"
            )

    for typed_ref in typed_refs:
        rendered = str(typed_ref)
        if rendered not in known_entities:
            warnings.append(f"unresolved reference {rendered}")

    return errors, warnings
