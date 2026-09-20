"""Value objects for the dataplane streaming surface (ADR 0130/0131, Plan 2).

A stream binding connects a named project/model/alias combination to an inbound connector (Kafka,
HTTP push, Dataplane bus req/res, ...) and carries the limits governing how its traffic is admitted and
retried. ``StreamRequest``/``InferenceResult`` are the connector-agnostic request/response shapes
that carry a request from ingress to the model router and back. All types are frozen dataclasses —
pure value objects, no I/O, no heavy imports — so later tasks (ingress, catalog, supervisor, the
Kafka stream) can share one vocabulary without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from examlops.dataplane.types import SpecError

#: The unscoped/default project. Stored as the empty string; ``_global`` is only ever a display
#: label (the same convention ``dataplane_sources``/``dataplane_pulls`` and every dataplane CLI
#: command already use). Re-exported by
#: :mod:`examlops.dataplane.streams.bindings` — the catalog module — so callers that already import
#: it keep working; this module is where it lives, because it is the one place every stream module
#: (bindings, ingress, kafka_stream, dlq, supervisor, the service routes) can import without
#: pulling in ``yaml`` or the datastore (review M3: five copies of the same two lines).
GLOBAL_PROJECT = ""

#: Terminal outcome of routing one :class:`StreamRequest` through the model router.
#:
#: ``ok`` a prediction was returned; ``model`` the model itself raised/failed; ``validation`` the
#: payload failed schema/size/NaN validation; ``transport`` the call to the model service failed
#: (network, connection refused, ...); ``overloaded`` admission control rejected the request;
#: ``deadline`` the request's deadline elapsed; ``not_found`` the stream/model/alias is unknown;
#: ``unexpected`` any other, unclassified failure.
Outcome = Literal[
    "ok",
    "model",
    "validation",
    "transport",
    "overloaded",
    "deadline",
    "not_found",
    "unexpected",
]


@dataclass(frozen=True)
class StreamRequest:
    """One inbound inference request, independent of which connector delivered it."""

    stream: str
    model: str
    alias: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    traceparent: str | None = None
    deadline_ms: int | None = None


@dataclass(frozen=True)
class InferenceResult:
    """Result of routing one :class:`StreamRequest` through the model router."""

    outcome: Outcome
    prediction: Any = None
    body: dict[str, Any] = field(default_factory=dict)
    retry_after: float | None = None
    status: int = 200


@dataclass(frozen=True)
class StreamLimits:
    """Admission and retry limits for one stream binding.

    Defaults are permissive-but-bounded so a caller can construct a binding without setting every
    field: bounded in-flight concurrency, no rate cap, no deadline, a 1 MiB body cap, and a handful
    of delivery retries.
    """

    max_in_flight: int = 64
    rate_per_min: int = 0  # 0 = unlimited
    deadline_ms: int | None = None
    max_bytes: int = 1_048_576
    max_attempts: int = 5


@dataclass(frozen=True)
class StreamBinding:
    """A named, project-scoped binding of one inbound connector to one model/alias.

    ``connection`` names a Named Connection (credentials never live on the binding itself).
    ``options`` is connector- and binding-specific free-form config; the one key every consumer of
    this module must know about is ``options["passthrough"]`` — a list of payload keys copied into
    the request body verbatim, in addition to the model's schema fields (see
    ``examlops.dataplane.streams.schema.build_body``).
    """

    project: str
    name: str
    connector: str
    model: str
    alias: str
    address: str
    connection: str | None
    options: dict[str, Any] = field(default_factory=dict)
    limits: StreamLimits = field(default_factory=StreamLimits)
    state: str = "enabled"
    origin: str = "api"


def normalize_project(project: Any) -> str:
    """The stored spelling of *project*: ``""`` and ``"_global"`` both name the unscoped default;
    every other value is stripped and passes through.

    Rejects a non-``str``/``None`` with :class:`~examlops.dataplane.types.SpecError` — a pack
    YAML's ``project:`` key holds whatever YAML parsed (``project: 123`` is an ``int``), and
    letting that reach ``.strip()`` used to raise an uncaught ``AttributeError``.
    """
    if project is None:
        return GLOBAL_PROJECT
    if not isinstance(project, str):
        raise SpecError(f"invalid project {project!r}: must be a string")
    p = project.strip()
    return GLOBAL_PROJECT if p == "_global" else p


def display_project(project: str | None) -> str:
    """The display label of a stored project: ``_global`` for the unscoped default."""
    return project or "_global"
