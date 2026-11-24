"""Shared store loader — used by pipeline.py and mcp_server.py."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from pydantic import BaseModel

from schemas.vocabulary import Vocabulary


class Store(dict):
    """kind -> {id -> model}.

    ``skipped`` lists the entity files that did not load, one dict each:
    ``{kind, id, path, error}``. One hand-edited service record with a value the
    schema refuses would otherwise make every reader of the store fail. A bad
    record is that record's problem: it is left out, named here and on stderr,
    and the rest loads.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.skipped: list[dict[str, str]] = []


def _first_line(exc: Exception) -> str:
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    return f"{type(exc).__name__}: {' | '.join(lines[:3])}"[:400]


def load_entity_file(yaml_path: Path, validator: Callable[[Any], BaseModel]) -> BaseModel:
    """One entity file as its model. Raises on unreadable YAML, a schema
    refusal, or a missing id — the same three ways a record can be bad."""
    model = validator(_load_yaml(yaml_path))
    model_id = getattr(model, "id", None)
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(f"Entity file missing string id: {yaml_path}")
    return model


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _iter_yaml_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in {".yaml", ".yml"}
        if p.is_file()
    )


def _schema_for_entity_dir(entity_dir_name: str) -> Callable[[Any], BaseModel] | None:
    singular = entity_dir_name[:-1] if entity_dir_name.endswith("s") else entity_dir_name
    module_name = f"schemas.{singular}"
    class_name = "".join(part.capitalize() for part in singular.split("_"))

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise

    schema_cls = getattr(module, class_name, None)
    if schema_cls is None or not hasattr(schema_cls, "model_validate"):
        return None

    return schema_cls.model_validate


def load_store(repo_root: Path, *, strict: bool = False) -> Store:
    """Load all vocabularies and entities from the store root into a typed dict.

    An entity file that does not load is skipped and listed in ``store.skipped``
    (``strict=True`` raises instead). A bad
    vocabulary always raises: nothing can be validated without it.
    """
    store = Store({"vocabulary": {}})

    vocab_root = repo_root / "vocabularies"
    for yaml_path in _iter_yaml_files(vocab_root):
        model = Vocabulary.model_validate(_load_yaml(yaml_path))
        store["vocabulary"][model.id] = model

    entities_root = repo_root / "entities"
    if entities_root.exists():
        for entity_dir in sorted(p for p in entities_root.iterdir() if p.is_dir()):
            entity_dir_name = entity_dir.name
            kind = entity_dir_name[:-1] if entity_dir_name.endswith("s") else entity_dir_name

            validator = _schema_for_entity_dir(entity_dir_name)
            if validator is None:
                print(
                    f"WARN: no schema module for entities/{entity_dir_name}/, skipping",
                    file=sys.stderr,
                )
                continue

            kind_store = store.setdefault(kind, {})
            for yaml_path in _iter_yaml_files(entity_dir):
                try:
                    model = load_entity_file(yaml_path, validator)
                except Exception as exc:  # noqa: BLE001 — one bad record must not take the store down
                    if strict:
                        raise
                    rel = str(yaml_path.relative_to(repo_root))
                    store.skipped.append({"kind": kind, "id": yaml_path.stem, "path": rel,
                                          "error": _first_line(exc)})
                    print(f"WARN: invalid Atlas record skipped: {rel}: {_first_line(exc)}",
                          file=sys.stderr)
                    continue
                kind_store[model.id] = model

    return store
