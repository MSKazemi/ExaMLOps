"""One ``JobRequest``, every execution adapter: the cross-adapter mapping (ADR 0116 verification 1).

The admission seam speaks the neutral vocabulary of :class:`~.request.JobRequest`; the execution
seam (``SchedulerAdapter.submit_job``) takes a free-form ``resources`` dict that each backend turns
into its own flags. This module is the single place where one becomes the other, so the claim
*"the same ``JobRequest`` round-trips through the mock, Slurm and Flux adapters unchanged"* is a
property of code that can be tested rather than of three hand-written call sites.

A translation has two halves:

* **native** — the ``resources`` dict handed to the adapter. It carries only what the backend
  itself enforces: node count, GPUs, CPUs per task, memory (Slurm only — flux-core's basic
  scheduler has no schedulable memory, so its adapter drops ``mem``), the queue, and a wall-time
  limit derived from ``est_runtime_s``.
* **envelope** — the canonical request as JSON. Everything a backend cannot express (gang,
  network tier, scale-up domain, priority class, flexibility, deadline, the project and tenant the
  quota is held against) lives here, and nowhere is it guessed back from native flags.

:func:`from_native` rebuilds the request from the envelope and **verifies** the native half against
it: a native value that disagrees with the envelope (an adapter that dropped ``--gpus``, a flag
someone edited, a lossy conversion) raises :class:`TranslationMismatch` instead of silently
producing a different job. :func:`parse_argv` reads the native half back from the exact argv an
adapter emitted, which is how the tests prove the round trip through the real adapter code.

``not_native`` lists, per backend, the fields the backend cannot enforce. It is the honest answer to
"what does this scheduler *not* promise for my request" — the ADR 0109 capability rule applied to
the vocabulary rather than to verbs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from .request import JobRequest

BACKENDS = ("mock", "slurm", "flux")

#: Neutral fields no execution backend enforces natively; they always travel in the envelope.
_ENVELOPE_ONLY = (
    "project",
    "tenant",
    "workload_class",
    "gang",
    "network_tier",
    "scale_up_domain",
    "priority_class",
    "deadline",
    "flexibility_s",
)


class TranslationError(ValueError):
    """The request cannot be translated for this backend (unknown backend, bad native input)."""


class TranslationMismatch(TranslationError):
    """The native half disagrees with the envelope: the job the backend saw is not the request."""

    def __init__(self, backend: str, problems: list[str]):
        self.backend = backend
        self.problems = list(problems)
        super().__init__(f"{backend}: " + "; ".join(problems))


def _check_backend(backend: str) -> str:
    name = (backend or "").strip().lower()
    if name not in BACKENDS:
        raise TranslationError(f"unknown backend {backend!r}; choose one of {list(BACKENDS)}")
    return name


def _hms(seconds: float) -> str:
    """Whole seconds, rounded **up** (a limit shorter than the estimate kills the job), H:MM:SS."""
    total = int(math.ceil(max(0.0, float(seconds))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _seconds(value: Any) -> int:
    """Parse a wall-time back to whole seconds: ``H:MM:SS`` / ``MM:SS`` / ``Ns`` / bare minutes."""
    text = str(value).strip()
    if text.endswith("s") and text[:-1].isdigit():
        return int(text[:-1])
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3:]
        return h * 3600 + m * 60 + s
    if text.isdigit():  # Slurm and Flux both read a bare number as minutes
        return int(text) * 60
    raise TranslationError(f"unparseable wall time {value!r}")


def _mem_mb(memory_gb: float) -> int:
    return int(math.ceil(float(memory_gb) * 1024))


def not_native(backend: str) -> list[str]:
    """The request fields ``backend`` cannot enforce itself (carried in the envelope only)."""
    name = _check_backend(backend)
    out = list(_ENVELOPE_ONLY)
    if name == "flux":
        out.append("resources.memory_gb")
    if name == "mock":  # the mock records resources but enforces none of them
        out += ["resources.gpus", "resources.cpus", "resources.memory_gb", "resources.nodes"]
        out += ["queue", "est_runtime_s"]
    return out


def to_native(request: JobRequest, backend: str) -> dict[str, Any]:
    """The ``resources`` dict ``backend``'s adapter consumes for ``request``.

    Keys are the adapters' own vocabulary (``adapter._RESOURCE_FLAGS`` for Slurm,
    ``FluxAdapter._flux_flags`` for Flux; the mock records the dict verbatim). A zero or absent
    value is omitted rather than written as ``0``: ``--gpus=0`` is not what "no GPUs" means to
    every Slurm site.
    """
    name = _check_backend(backend)
    request.validate()
    r = request.resources
    out: dict[str, Any] = {"nodes": r.nodes}
    if r.gpus:
        if name == "flux":
            # Flux's ``-g`` is GPUs *per slot*, and the adapter's ``flux batch -N<n>`` (no ``-n``)
            # has one slot per node: writing the job total there would allocate total x nodes.
            if r.gpus % r.nodes:
                raise TranslationError(
                    f"flux: {r.gpus} GPUs cannot be spread evenly over {r.nodes} nodes "
                    "(the adapter's -g is GPUs per node-slot)"
                )
            out["gpus"] = r.gpus // r.nodes
        else:
            out["gpus"] = r.gpus  # Slurm ``--gpus`` is the job total
    if r.cpus:
        out["cpus_per_task"] = r.cpus
    if r.memory_gb and name != "flux":
        out["mem"] = f"{_mem_mb(r.memory_gb)}M"
    if request.queue:
        # Slurm's queue is a partition; the Flux adapter maps its ``qos`` key to ``--queue``.
        out["qos" if name == "flux" else "partition"] = request.queue
    if request.est_runtime_s is not None:
        out["time"] = _hms(request.est_runtime_s)
    return out


def envelope(request: JobRequest) -> str:
    """The canonical request as compact, key-sorted JSON (stable across processes)."""
    return json.dumps(request.validate().to_dict(), sort_keys=True, separators=(",", ":"))


def translate(request: JobRequest, backend: str) -> dict[str, Any]:
    """``{"backend", "native", "envelope", "not_native"}`` — everything a submission needs."""
    name = _check_backend(backend)
    return {
        "backend": name,
        "native": to_native(request, name),
        "envelope": envelope(request),
        "not_native": not_native(name),
    }


def _expected(request: JobRequest, backend: str) -> dict[str, Any]:
    """What the native half must say once it has been through an adapter and back."""
    exp = to_native(request, backend)
    if "time" in exp:
        exp["time"] = _seconds(exp["time"])
    if "mem" in exp:
        exp["mem"] = _mem_mb(request.resources.memory_gb)
    return exp


def _normalize_native(native: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in native.items():
        if key in ("nodes", "gpus", "cpus_per_task"):
            try:
                out[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise TranslationError(f"native {key}={value!r} is not an integer") from exc
        elif key == "time":
            out[key] = _seconds(value)
        elif key == "mem":
            text = str(value).strip().upper()
            if text.endswith("G"):
                out[key] = int(float(text[:-1]) * 1024)
            elif text.endswith("M"):
                out[key] = int(float(text[:-1]))
            else:
                out[key] = int(float(text))  # Slurm's default unit is MB
        else:
            out[key] = value
    return out


def from_native(native: dict[str, Any], envelope_json: str, backend: str) -> JobRequest:
    """Rebuild the request from ``envelope_json`` and verify ``native`` against it.

    Only the keys this module writes are compared; adapter bookkeeping (``output``, ``error``,
    ``job_name``, ``account``) is ignored. Raises :class:`TranslationMismatch` listing every
    disagreement.
    """
    name = _check_backend(backend)
    try:
        data = json.loads(envelope_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TranslationError(f"envelope is not JSON: {exc}") from exc
    request = JobRequest.from_dict(data)
    # The mock enforces nothing, but it records what it was given, and that must still agree.
    expected = _expected(request, name)
    got = _normalize_native({k: v for k, v in native.items() if k in _NATIVE_KEYS})
    problems = [
        f"{key}: native {got.get(key)!r} != request {expected.get(key)!r}"
        for key in sorted(set(expected) | set(got))
        if got.get(key) != expected.get(key)
    ]
    if problems:
        raise TranslationMismatch(name, problems)
    return request


_NATIVE_KEYS = frozenset({"nodes", "gpus", "cpus_per_task", "mem", "partition", "qos", "time"})

#: Slurm long flags -> native key (the inverse of ``adapter._RESOURCE_FLAGS`` for our keys).
_SLURM_FLAGS = {
    "--nodes": "nodes",
    "--gpus": "gpus",
    "--cpus-per-task": "cpus_per_task",
    "--mem": "mem",
    "--partition": "partition",
    "--time": "time",
}
#: Flux short flags -> native key (the inverse of ``FluxAdapter._flux_flags``).
_FLUX_SHORT = {"-N": "nodes", "-g": "gpus", "-c": "cpus_per_task", "-t": "time"}


def parse_argv(backend: str, argv: Sequence[str]) -> dict[str, Any]:
    """The native half as the scheduler will actually see it, read from an adapter's argv."""
    name = _check_backend(backend)
    out: dict[str, Any] = {}
    if name == "slurm":
        for arg in argv:
            flag, sep, value = str(arg).partition("=")
            if sep and flag in _SLURM_FLAGS:
                out[_SLURM_FLAGS[flag]] = value
        return out
    if name == "flux":
        for arg in argv:
            text = str(arg)
            if text.startswith("--queue="):
                out["qos"] = text.split("=", 1)[1]
                continue
            prefix = text[:2]
            if prefix in _FLUX_SHORT and len(text) > 2 and not text.startswith("--"):
                out[_FLUX_SHORT[prefix]] = text[2:]
        return out
    raise TranslationError("the mock adapter runs no command; read its recorded resources dict")
