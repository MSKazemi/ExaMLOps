"""Connector registry: lazy built-ins + ``exa.dataplane.connectors`` entry-point plugins."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any

from examlops.dataplane.connectors.base import Connector
from examlops.dataplane.types import SpecError

ENTRY_POINT_GROUP = "exa.dataplane.connectors"

# module:Class paths — imported lazily; each module imports its heavy deps inside methods, so a
# missing extra shows up as available() == False rather than an ImportError here.
_BUILTINS: dict[str, str] = {
    "sql": "examlops.dataplane.connectors.sql:SqlConnector",
    "files": "examlops.dataplane.connectors.files:FilesConnector",
    "zenodo": "examlops.dataplane.connectors.zenodo:ZenodoConnector",
    "rest": "examlops.dataplane.connectors.rest:RestConnector",
    "kafka": "examlops.dataplane.connectors.kafka:KafkaConnector",
}

_registry: dict[str, Connector] = {}
_plugin_errors: dict[str, str] = {}
_loaded = False


def _entry_points() -> list[Any]:
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def _load_path(path: str) -> Any:
    module, attr = path.split(":")
    return getattr(importlib.import_module(module), attr)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for kind, path in _BUILTINS.items():
        _registry.setdefault(kind, _load_path(path)())
    for ep in _entry_points():
        try:
            obj = ep.load()
            inst = obj() if isinstance(obj, type) else obj
            _registry.setdefault(inst.kind, inst)
        except Exception as exc:  # a broken plugin must not take the registry down
            _plugin_errors[ep.name] = f"{type(exc).__name__}: {exc}"


def register(connector: Connector, *, replace: bool = False) -> None:
    _ensure_loaded()
    if connector.kind in _registry and not replace:
        raise SpecError(f"connector {connector.kind!r} is already registered")
    _registry[connector.kind] = connector


def get(kind: str) -> Connector:
    _ensure_loaded()
    try:
        return _registry[kind]
    except KeyError:
        raise SpecError(
            f"unknown connector {kind!r}; known: {', '.join(sorted(_registry))}"
        ) from None


def all_connectors() -> list[Connector]:
    _ensure_loaded()
    return [_registry[k] for k in sorted(_registry)]


def connection_kinds() -> tuple[str, ...]:
    kinds: set[str] = set()
    for c in all_connectors():
        kinds.update(c.connection_kinds)
    return tuple(sorted(kinds))


def plugin_errors() -> dict[str, str]:
    _ensure_loaded()
    return dict(_plugin_errors)


def reset() -> None:
    """Forget everything (tests)."""
    global _loaded
    _registry.clear()
    _plugin_errors.clear()
    _loaded = False
