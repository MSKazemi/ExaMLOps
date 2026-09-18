"""The substrate seam: one contract for *where* a servable runs (ADR 0142 d1, spec-usar-1 §4.1).

``ServingBackend`` (render/deploy/rollout) and ``EndpointLauncher`` (start/stop/status) described the
same thing twice. A :class:`Substrate` has one shape on every target:

* ``render(spec, resolved)`` — **pure**: same inputs, byte-identical :class:`Rendered`; everything that
  needed the world (registry, cluster) arrived in the :class:`~.resolve.ResolvedRef` beforehand;
* ``apply(rendered, dry_run=…, plan_hash=…)`` — a dry run changes nothing and needs no plan; a real
  apply acts only on the exact plan it was shown (``plan_hash``) and writes exactly one audit event;
* ``status`` / ``stop`` / ``capabilities``.

A servable that needs a capability the substrate does not advertise is refused with
:class:`CapabilityMissing` — never silently degraded (ADR 0140 d7).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from examlops.serving.substrates.resolve import RenderError, ResolvedRef

__all__ = [
    "ApplyFailed",
    "ApplyResult",
    "CapabilityMissing",
    "PlanMismatch",
    "RenderError",
    "Rendered",
    "Substrate",
    "SubstrateCaps",
    "SubstrateError",
    "SubstrateStatus",
    "SubstrateUnavailable",
    "TrafficSplit",
    "audited_apply",
    "content_hash",
    "require_caps",
    "servable_kind",
]


class SubstrateError(Exception):
    """Base of the seam's typed errors; ``exit_code`` is what the CLI envelope reports."""

    exit_code = 10


class SubstrateUnavailable(SubstrateError):
    """The substrate cannot act here (not built yet, or its backend is unreachable)."""

    exit_code = 11


class CapabilityMissing(SubstrateError):
    """The servable needs something the bound substrate does not advertise."""

    exit_code = 12

    def __init__(self, capability: str, substrate: str, hint: str = "") -> None:
        self.capability, self.substrate = capability, substrate
        super().__init__(
            f"substrate {substrate!r} does not provide {capability!r}"
            + (f": {hint}" if hint else "")
        )


class PlanMismatch(SubstrateError):
    """A real apply was asked to act on something other than the plan it was shown."""

    exit_code = 13


class ApplyFailed(SubstrateError):
    """The substrate accepted the plan but could not carry it out."""

    exit_code = 14


@dataclass(frozen=True)
class SubstrateCaps:
    kinds: frozenset[str]
    canary: bool = False
    shadow: bool = False
    scale_to_zero: bool = False
    kv_routing: bool = False
    pd: bool = False
    multinode: bool = False
    gpu: bool = False
    accelerators: frozenset[str] = frozenset()
    oci_delivery: frozenset[str] = frozenset()
    multi_model_density: bool = False
    live_apply: bool = False
    isolation: str | None = None


@dataclass(frozen=True)
class TrafficSplit:
    """The one traffic intent every substrate renders its own way (ADR 0142 d5)."""

    stable_version: str
    canary_version: str | None = None
    canary_percent: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.canary_percent <= 100:
            raise RenderError(f"canary percent {self.canary_percent} is outside 0..100")
        if self.canary_version is not None and self.canary_version == self.stable_version:
            raise RenderError(f"canary version {self.canary_version} is the stable version")


@dataclass(frozen=True)
class Rendered:
    substrate: str
    objects: tuple[dict[str, Any], ...]
    content_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    substrate: str
    dry_run: bool
    plan_hash: str | None
    applied: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubstrateStatus:
    state: str  # PENDING | STARTING | READY | DEGRADED | STOPPED | FAILED | UNKNOWN
    versions: dict[str, int] = field(default_factory=dict)
    address: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Substrate(Protocol):
    name: str

    def capabilities(self) -> SubstrateCaps: ...
    def render(self, spec: dict[str, Any], resolved: ResolvedRef) -> Rendered: ...
    def apply(
        self, rendered: Rendered, *, dry_run: bool = True, plan_hash: str | None = None
    ) -> ApplyResult: ...
    def status(self, name: str) -> SubstrateStatus: ...
    def stop(self, name: str) -> None: ...


# ── helpers every implementation shares ───────────────────────────────────────


def content_hash(objects: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    """sha256 over canonical JSON — the identity of a render, and so of the plan built on it."""
    blob = json.dumps(list(objects), sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def make_rendered(
    substrate: str, objects: list[dict[str, Any]], warnings: list[str] | None = None
) -> Rendered:
    frozen = tuple(json.loads(json.dumps(o, sort_keys=True)) for o in objects)
    return Rendered(substrate, frozen, content_hash(frozen), tuple(warnings or ()))


def servable_kind(spec: dict[str, Any]) -> str:
    """``predictive`` | ``generative`` | ``agentic`` — declared, else derived (ADR 0140 d3)."""
    declared = spec.get("kind")
    if declared:
        kind = str(declared).lower()
        if kind not in ("predictive", "generative", "agentic"):
            raise RenderError(f"unknown servable kind {declared!r}")
        return kind
    from examlops.serving.substrates.kserve import is_generative

    return "generative" if is_generative(spec) else "predictive"


def require_caps(caps: SubstrateCaps, substrate: str, spec: dict[str, Any]) -> None:
    """Refuse a servable whose needs the substrate does not advertise (never degrade)."""
    kind = servable_kind(spec)
    if kind not in caps.kinds:
        hint = (
            "use the 'k8s-agents' substrate" if kind == "agentic" and substrate == "kserve" else ""
        )
        raise CapabilityMissing(f"kind:{kind}", substrate, hint)
    scaling = spec.get("scaling") or {}
    if scaling.get("min_replicas") == 0 and not caps.scale_to_zero:
        raise CapabilityMissing("scale_to_zero", substrate)
    if (spec.get("rollout") or {}).get("canary") and not caps.canary:
        raise CapabilityMissing("canary", substrate)


def audited_apply(
    substrate: str,
    rendered: Rendered,
    *,
    dry_run: bool,
    plan_hash: str | None,
    act: Any,
) -> ApplyResult:
    """The apply rule every substrate shares (spec-usar-1 R-SUB-3).

    A dry run returns without touching anything and without a plan. A real apply refuses unless
    ``plan_hash`` is the rendered content's own hash, runs ``act()`` (which returns the refs it
    changed), and writes exactly one audit event — success or failure.
    """
    if dry_run:
        return ApplyResult(substrate, True, plan_hash, (), {"would_apply": rendered.content_hash})
    if plan_hash != rendered.content_hash:
        raise PlanMismatch(
            f"plan {plan_hash!r} is not the rendered content {rendered.content_hash!r}; re-plan"
        )
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    target = _target_of(rendered)
    with _span(substrate, target) as span:
        try:
            applied = tuple(act())
        except SubstrateError as exc:
            _audit(actor, substrate, target, plan_hash, f"failed: {exc}")
            span.set_attribute("examlops.substrate.result", "failed")
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as a typed error
            _audit(actor, substrate, target, plan_hash, f"failed: {exc}")
            span.set_attribute("examlops.substrate.result", "failed")
            raise ApplyFailed(str(exc)) from exc
        _audit(actor, substrate, target, plan_hash, "applied", applied)
        span.set_attribute("examlops.substrate.result", "applied")
    return ApplyResult(substrate, False, plan_hash, applied)


class _NoSpan:
    def set_attribute(self, *_a: Any) -> None:
        pass


@contextmanager
def _span(substrate: str, target: str) -> Iterator[Any]:
    """One ``substrate.apply`` span per real apply (R-SUB-48); a no-op unless OTel is enabled."""
    from examlops.observability import tracing_enabled

    if not tracing_enabled():
        yield _NoSpan()
        return
    from opentelemetry import trace

    tracer = trace.get_tracer("examlops.substrate")
    with tracer.start_as_current_span(
        "substrate.apply",
        attributes={"examlops.substrate": substrate, "examlops.servable": target},
    ) as span:
        yield span


def _target_of(rendered: Rendered) -> str:
    for obj in rendered.objects:
        labels = (obj.get("metadata") or {}).get("labels") or obj.get("labels") or {}
        if labels.get("examlops.io/model"):
            return f"{labels['examlops.io/model']}@{labels.get('examlops.io/version', '?')}"
    return rendered.content_hash[:19]


def _audit(
    actor: str, substrate: str, target: str, plan: str, result: str, refs: tuple[str, ...] = ()
) -> None:
    from examlops.data.audit import write_audit_event

    write_audit_event(
        "substrate",
        actor,
        "substrate_apply",
        target,
        {"substrate": substrate, "plan_hash": plan, "result": result, "applied": list(refs)},
    )
