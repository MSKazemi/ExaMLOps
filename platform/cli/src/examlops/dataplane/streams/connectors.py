"""Stream connector registry (ADR 0131, Plan 2, task A7).

Mirrors Plan 1's batch registry (:mod:`examlops.dataplane.connectors.registry`): lazily imported
built-ins, plus third-party connectors from the ``exa.dataplane.stream_connectors`` entry-point
group. ``register(c, replace=False)`` refuses a duplicate kind and ``get(kind)`` an unknown one,
both with :class:`~examlops.dataplane.types.SpecError`. A broken plugin is recorded in
:func:`plugin_errors` and never takes the registry down. Plugins are found through installed
package metadata only — nothing here touches ``sys.path``.

Two differences from the batch registry, both additive: a plugin that is not shaped like a
:class:`StreamConnector`, or that claims a kind already registered, is recorded in
:func:`plugin_errors` rather than skipped silently; and loading is serialised by a lock, because
the supervisor resolves connectors from one thread per stream.

A stream connector is a long-running loop, not a bounded read: the supervisor (A8) calls
``run(binding, ingress, stop_event, status_cb)`` on its own thread. ``run`` returns cleanly once
``stop_event`` is set (after committing whatever it must commit) and returns or raises on a fatal
error; reconnect and backoff are the supervisor's job. ``status_cb(state, detail)`` reports one
of :data:`STATES`.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from examlops.dataplane.safety import redact
from examlops.dataplane.streams.types import StreamBinding
from examlops.dataplane.types import SpecError

if TYPE_CHECKING:
    from examlops.dataplane.streams.ingress import StreamIngress

ENTRY_POINT_GROUP = "exa.dataplane.stream_connectors"

#: Every state a stream can report — through a connector's ``status_cb`` or from the supervisor
#: itself (``standby`` for a follower that lost the leader election, ``stopped`` for a stream it
#: stopped or never started). One vocabulary, so a reader of ``dataplane_stream_connector_state``
#: knows the whole label set; :meth:`examlops.dataplane.streams.supervisor._Stream._set_state`
#: checks against it and logs an unknown state rather than publishing it silently (review M1 —
#: the tuple used to omit ``standby``, which the supervisor has always emitted).
STATES = ("starting", "running", "paused", "retrying", "standby", "error", "stopped")

#: ``status_cb(state, detail)`` — ``state`` is one of :data:`STATES`; ``detail`` is a short,
#: redacted, human-readable note or ``None``.
StatusCallback = Callable[[str, str | None], None]


@runtime_checkable
class StreamConnector(Protocol):
    """An inbound live-stream connector.

    ``kind`` names it in a binding's ``connector`` field; ``singleton`` asks the supervisor to run
    at most one instance per stream across replicas (behind a lease) — ``False`` when the source
    already balances consumers itself, as a Kafka consumer group does; ``connection_kinds`` lists
    the Named Connection kinds a binding may reference.
    """

    kind: str
    singleton: bool
    connection_kinds: tuple[str, ...]

    def run(
        self,
        binding: StreamBinding,
        ingress: StreamIngress,
        stop_event: threading.Event,
        status_cb: StatusCallback,
    ) -> None: ...


# module:Class paths, imported on first use. A built-in imports its heavy client library inside
# its methods, so a missing extra never breaks the registry.
_BUILTINS: dict[str, str] = {
    "kafka": "examlops.dataplane.streams.kafka_stream:KafkaStreamConnector",
}

_registry: dict[str, StreamConnector] = {}
_plugin_errors: dict[str, str] = {}
_loaded = False
_lock = threading.RLock()


def _entry_points() -> list[Any]:
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def _load_path(path: str) -> Any:
    module, attr = path.split(":")
    return getattr(importlib.import_module(module), attr)


def _shape_error(obj: Any) -> str | None:
    """Why ``obj`` is not a usable :class:`StreamConnector`, or ``None`` when it is."""
    kind = getattr(obj, "kind", None)
    if not isinstance(kind, str) or not kind:
        return "kind must be a non-empty string"
    if not isinstance(getattr(obj, "singleton", None), bool):
        return "singleton must be a bool"
    kinds = getattr(obj, "connection_kinds", None)
    if not isinstance(kinds, tuple) or not all(isinstance(k, str) for k in kinds):
        return "connection_kinds must be a tuple of strings"
    if not callable(getattr(obj, "run", None)):
        return "run must be callable"
    return None


def _ensure_loaded() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        for kind, path in _BUILTINS.items():
            try:
                _registry.setdefault(kind, _load_path(path)())
            except Exception as exc:  # noqa: BLE001 - one broken built-in must not hide the rest
                _plugin_errors[f"builtin:{kind}"] = _error_text(exc)
        for ep in _entry_points():
            try:
                obj = ep.load()
                inst = obj() if isinstance(obj, type) else obj
                problem = _shape_error(inst)
                if problem is not None:
                    _plugin_errors[ep.name] = f"not a stream connector: {problem}"
                    continue
                if inst.kind in _registry:
                    _plugin_errors[ep.name] = (
                        f"stream connector {inst.kind!r} is already registered"
                    )
                    continue
                _registry[inst.kind] = inst
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not take the registry down
                _plugin_errors[ep.name] = _error_text(exc)


def _error_text(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


def register(connector: StreamConnector, *, replace: bool = False) -> None:
    problem = _shape_error(connector)
    if problem is not None:
        raise SpecError(f"not a stream connector: {problem}")
    _ensure_loaded()
    with _lock:
        if connector.kind in _registry and not replace:
            raise SpecError(f"stream connector {connector.kind!r} is already registered")
        _registry[connector.kind] = connector


def get(kind: str) -> StreamConnector:
    _ensure_loaded()
    with _lock:
        try:
            return _registry[kind]
        except KeyError:
            raise SpecError(
                f"unknown stream connector {kind!r}; known: {', '.join(sorted(_registry))}"
            ) from None


def all_connectors() -> list[StreamConnector]:
    _ensure_loaded()
    with _lock:
        return [_registry[k] for k in sorted(_registry)]


def connection_kinds() -> tuple[str, ...]:
    kinds: set[str] = set()
    for c in all_connectors():
        kinds.update(c.connection_kinds)
    return tuple(sorted(kinds))


def plugin_errors() -> dict[str, str]:
    _ensure_loaded()
    with _lock:
        return dict(_plugin_errors)


def reset() -> None:
    """Forget everything (tests)."""
    global _loaded
    with _lock:
        _registry.clear()
        _plugin_errors.clear()
        _loaded = False


__all__ = [
    "ENTRY_POINT_GROUP",
    "STATES",
    "StatusCallback",
    "StreamConnector",
    "all_connectors",
    "connection_kinds",
    "get",
    "plugin_errors",
    "register",
    "reset",
]
