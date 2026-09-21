"""Optional capability probe for the execution seam (ADR 0116 decision 1, ADR 0109 honesty rule).

``SchedulerAdapter`` is *not* changed: adding a member to a ``runtime_checkable`` Protocol would
make every existing adapter fail ``isinstance``. Instead this module inspects an adapter from the
outside and reports what it can do, each answer being ``True`` / ``False`` / ``None`` (unknown).
Unknown is the default and is treated as *no promise* by every caller: the broker refuses to
promise gang scheduling, preemption or reservations on a backend that has not said it can.

An adapter opts in by defining ``capabilities() -> dict`` (or an ``AdapterCapabilities``); the
in-repo mock adapter is registered below with what it truly does (nothing beyond a fake queue).
Slurm and Flux are deliberately left ``None``: the underlying schedulers can do more than the
adapters expose, and asserting either way from here would be a guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


class PreemptUnsupported(RuntimeError):
    """The backend cannot (or has not said it can) preempt; nothing was killed or paused."""


@dataclass(frozen=True)
class AdapterCapabilities:
    supports_gang: bool | None = None
    supports_preempt: bool | None = None
    supports_reservations: bool | None = None
    source: str = "default-unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class PreemptCapable(Protocol):
    """The *optional* execution-seam verb. Adapters that lack it are not SchedulerAdapters any
    less; they simply refuse (:func:`preempt`)."""

    def preempt(self, job_id: str, *, checkpoint: bool = True) -> str: ...


#: type name -> capabilities, for adapters that cannot be edited to describe themselves.
_REGISTRY: dict[str, AdapterCapabilities] = {
    # The mock runs the script inline (or fakes a queue): no gang, no preemption, no reservations.
    "MockSlurmAdapter": AdapterCapabilities(False, False, False, "registry:mock"),
}


def register_capabilities(type_name: str, caps: AdapterCapabilities) -> None:
    _REGISTRY[type_name] = caps


def probe(adapter: Any) -> AdapterCapabilities:
    """What ``adapter`` can do. Never raises; anything unstated is ``None`` (unknown)."""
    declared = getattr(adapter, "capabilities", None)
    if callable(declared):
        try:
            got = declared()
        except Exception:  # noqa: BLE001 - a broken self-description must not break admission
            got = None
        if isinstance(got, AdapterCapabilities):
            return got
        if isinstance(got, dict):
            return AdapterCapabilities(
                supports_gang=got.get("supports_gang"),
                supports_preempt=got.get("supports_preempt"),
                supports_reservations=got.get("supports_reservations"),
                source="adapter.capabilities()",
            )
    known = _REGISTRY.get(type(adapter).__name__)
    if known is not None:
        return known
    return AdapterCapabilities()


def preempt(adapter: Any, job_id: str, *, checkpoint: bool = True) -> str:
    """Preempt ``job_id`` iff the backend says it can. Otherwise refuse - never fake it.

    A backend that does not advertise ``supports_preempt is True`` *and* implement ``preempt``
    gets a typed :class:`PreemptUnsupported` with the reason; the job is untouched.
    """
    caps = probe(adapter)
    if caps.supports_preempt is not True:
        state = "does not support" if caps.supports_preempt is False else "has not declared"
        raise PreemptUnsupported(
            f"{type(adapter).__name__} {state} preemption (supports_preempt="
            f"{caps.supports_preempt}); refusing rather than kill-and-restart job {job_id}"
        )
    if not isinstance(adapter, PreemptCapable):
        raise PreemptUnsupported(
            f"{type(adapter).__name__} advertises preemption but implements no preempt() verb"
        )
    return adapter.preempt(job_id, checkpoint=checkpoint)
