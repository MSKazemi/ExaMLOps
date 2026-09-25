"""The neutral job vocabulary of the admission seam (ADR 0116 decision 2).

A pure, frozen, versioned, JSON-serializable value. It carries the fields the *decision* before
execution needs and the opaque ``resources: dict`` of ``SchedulerAdapter.submit_job`` cannot
express as anything but free-form keys. Nothing here talks to a scheduler or a database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

NETWORK_TIERS = ("scale_up", "scale_out", "wan_tolerant")
SCALE_UP_DOMAINS = ("required", "preferred", "not_required")

#: Priority classes and the integer the legacy queue's ``priority`` column stores for them.
PRIORITY_CLASSES: dict[str, int] = {
    "best_effort": -10,
    "batch": 0,
    "standard": 10,
    "high": 50,
    "critical": 100,
}


class JobRequestError(ValueError):
    """A request that is not valid; ``problems`` lists every reason, not just the first."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class Resources:
    gpus: int = 0
    cpus: int = 0
    memory_gb: float = 0.0
    nodes: int = 1


@dataclass(frozen=True)
class JobRequest:
    """What a caller asks the admission seam to decide about."""

    project: str
    resources: Resources = field(default_factory=Resources)
    tenant: str = "default"
    workload_class: str = "training"
    gang: bool = False
    network_tier: str = "scale_out"
    scale_up_domain: str = "not_required"
    queue: str | None = None
    priority_class: str = "batch"
    #: ISO-8601 instant by which the job must have finished, or ``None``.
    deadline: str | None = None
    #: How long the job may be deferred (seconds). Read by the ``carbon`` admission gate
    #: (``gates.CarbonGate``); no fair-share policy shifts work on it.
    flexibility_s: float = 0.0
    #: Estimated run time, used only to size a GPU-hour reservation.
    est_runtime_s: float | None = None
    schema_version: int = SCHEMA_VERSION

    # ── validation ───────────────────────────────────────────────────────────────────────
    def problems(self) -> list[str]:
        out: list[str] = []
        r = self.resources
        if not isinstance(self.project, str) or not self.project.strip():
            out.append("project must be a non-empty string")
        if not isinstance(self.tenant, str) or not self.tenant.strip():
            out.append("tenant must be a non-empty string")
        if not isinstance(self.workload_class, str) or not self.workload_class.strip():
            out.append("workload_class must be a non-empty string")
        if self.schema_version != SCHEMA_VERSION:
            out.append(f"schema_version {self.schema_version!r} is not {SCHEMA_VERSION}")
        for name in ("gpus", "cpus", "nodes"):
            v = getattr(r, name)
            if not _is_int(v):
                out.append(f"resources.{name} must be an integer")
            elif v < 0:
                out.append(f"resources.{name} must be >= 0")
        if _is_int(r.nodes) and r.nodes < 1:
            out.append("resources.nodes must be >= 1")
        if not _is_num(r.memory_gb) or r.memory_gb < 0:
            out.append("resources.memory_gb must be a number >= 0")
        if not isinstance(self.gang, bool):
            out.append("gang must be a boolean")
        if self.network_tier not in NETWORK_TIERS:
            out.append(f"network_tier must be one of {list(NETWORK_TIERS)}")
        if self.scale_up_domain not in SCALE_UP_DOMAINS:
            out.append(f"scale_up_domain must be one of {list(SCALE_UP_DOMAINS)}")
        if self.scale_up_domain == "required" and self.network_tier != "scale_up":
            out.append("scale_up_domain 'required' needs network_tier 'scale_up'")
        if self.priority_class not in PRIORITY_CLASSES:
            out.append(f"priority_class must be one of {sorted(PRIORITY_CLASSES)}")
        if not _is_num(self.flexibility_s) or self.flexibility_s < 0:
            out.append("flexibility_s must be a number >= 0")
        if self.est_runtime_s is not None and (
            not _is_num(self.est_runtime_s) or self.est_runtime_s < 0
        ):
            out.append("est_runtime_s must be a number >= 0 or null")
        if self.deadline is not None:
            try:
                datetime.fromisoformat(str(self.deadline))
            except ValueError:
                out.append("deadline must be an ISO-8601 timestamp")
        if self.queue is not None and (not isinstance(self.queue, str) or not self.queue.strip()):
            out.append("queue must be a non-empty string or null")
        return out

    def validate(self) -> JobRequest:
        problems = self.problems()
        if problems:
            raise JobRequestError(problems)
        return self

    # ── serialization ────────────────────────────────────────────────────────────────────
    @property
    def priority(self) -> int:
        return PRIORITY_CLASSES.get(self.priority_class, 0)

    @property
    def gpu_hours(self) -> float:
        """Estimated GPU-hours, ``0.0`` when no runtime estimate was given."""
        if self.est_runtime_s is None:
            return 0.0
        return float(self.resources.gpus) * float(self.est_runtime_s) / 3600.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRequest:
        """Strict: unknown keys and a wrong shape are refused, never ignored."""
        if not isinstance(data, dict):
            raise JobRequestError(["a job request must be a JSON object"])
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(data) - known)
        if unknown:
            raise JobRequestError([f"unknown field(s): {unknown}"])
        payload = dict(data)
        res = payload.pop("resources", {})
        if not isinstance(res, dict):
            raise JobRequestError(["resources must be an object"])
        bad = sorted(set(res) - set(Resources.__dataclass_fields__))
        if bad:
            raise JobRequestError([f"unknown resources field(s): {bad}"])
        try:
            req = cls(resources=Resources(**res), **payload)
        except TypeError as exc:  # a missing required field (project)
            raise JobRequestError([str(exc)]) from exc
        return req.validate()
