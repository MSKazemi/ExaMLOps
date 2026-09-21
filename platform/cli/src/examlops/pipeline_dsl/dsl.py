"""The Python pipeline DSL: ``@pipeline`` plus step helpers that *build* the IR (ADR 0080).

Calling a ``@pipeline`` function runs its body once under a **tracing context**. Inside it the step
helpers (:func:`dataset`, :func:`train`, :func:`evaluate`, :func:`promote`, ...) do not execute
anything — each records a node in the IR and returns a :class:`Ref`, a handle to that step's typed
output. Passing a ``Ref`` to another step records the edge. The result is a validated, hashed IR
dict (:mod:`examlops.pipeline_dsl.ir`).

What this is and is not (say it plainly):

* Tracing is **declarative by convention, not by enforcement.** The body is ordinary Python and
  runs with the caller's full privileges — a pipeline file is *operator-written, trusted-tier code*,
  the same tier as an entry-point plugin (ADR 0081). Importing it can do anything Python can.
* There is no sandbox on this path. The opt-in ``--untrusted`` compile mode
  (:func:`examlops.pipeline_dsl.loader.load_pipeline_file` with ``sandboxed=True``) reuses the
  provider AST allow-list (no imports, no ``open``/``eval``/``getattr``, no dunder access), which
  is a static gate for authenticated authors — **not** a hardened jail.
* The helpers themselves have no side effects; outside a tracing context they raise.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ir import STEP_KINDS, IRError, build_ir, new_node

__all__ = [
    "Ref",
    "Resources",
    "PipelineDef",
    "pipeline",
    "step",
    "dataset",
    "train",
    "evaluate",
    "promote",
    "hpo",
    "custom_python",
]


@dataclass(frozen=True)
class Ref:
    """A handle to one output of a traced step. Only meaningful inside a ``@pipeline`` body."""

    node: str
    output: str
    type: str


@dataclass(frozen=True)
class Resources:
    """A scheduler-neutral resource ask (mirrors ``hpc_placement.ResourceAsk``)."""

    gpus: int = 0
    cpus: int = 0
    nodes: int = 1

    def as_dict(self) -> dict[str, int]:
        return {"gpus": self.gpus, "cpus": self.cpus, "nodes": self.nodes}


@dataclass
class _Tracer:
    name: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)

    def next_id(self, kind: str, hint: str | None) -> str:
        base = f"{kind}_{hint}" if hint else kind
        if base not in self.ids:
            return base
        n = 2
        while f"{base}_{n}" in self.ids:
            n += 1
        return f"{base}_{n}"


_TRACER: contextvars.ContextVar[_Tracer | None] = contextvars.ContextVar("exa_tracer", default=None)


def _tracer() -> _Tracer:
    t = _TRACER.get()
    if t is None:
        raise IRError(
            "pipeline steps only run inside a @pipeline function being compiled — "
            "call the decorated function (or .compile()) instead of the step helper directly"
        )
    return t


def _resources(value: Resources | dict[str, int] | None) -> dict[str, int]:
    if value is None:
        return {}
    return value.as_dict() if isinstance(value, Resources) else dict(value)


def step(
    kind: str,
    *,
    id: str | None = None,
    inputs: dict[str, Ref | list[Ref] | tuple[Ref, ...]] | None = None,
    resources: Resources | dict[str, int] | None = None,
    **params: Any,
) -> Ref:
    """Record one step of ``kind`` and return a :class:`Ref` to its first output.

    ``inputs`` maps a port name to a ``Ref`` (or a list of them for a ``many`` port); ``params``
    are the step's own settings. Everything is validated when the pipeline finishes tracing.
    """
    tracer = _tracer()
    spec = STEP_KINDS.get(kind)
    if spec is None:
        raise IRError(f"unknown step kind {kind!r} (known: {', '.join(sorted(STEP_KINDS))})")
    hint = str(params["name"]) if kind == "dataset" and "name" in params else None
    node_id = id or tracer.next_id(kind, hint)
    if node_id in tracer.ids:
        raise IRError(f"duplicate step id {node_id!r}")
    tracer.ids.add(node_id)
    tracer.nodes.append(new_node(node_id, kind, params, _resources(resources)))
    for port, refs in (inputs or {}).items():
        for ref in [refs] if isinstance(refs, Ref) else list(refs):
            if not isinstance(ref, Ref):
                raise IRError(
                    f"step {node_id!r}: input {port!r} must be the result of another step, "
                    f"got {type(ref).__name__}"
                )
            tracer.edges.append(
                {"from": ref.node, "output": ref.output, "to": node_id, "input": port}
            )
    output = next(iter(spec.outputs))
    return Ref(node_id, output, spec.outputs[output])


def dataset(name: str, *, id: str | None = None, **params: Any) -> Ref:
    """A dataset the pipeline reads (``backend``, ``cache_dir``, ``splits``, ... as in the YAML)."""
    return step("dataset", id=id, name=name, **params)


def train(
    *datasets: Ref,
    id: str | None = None,
    resources: Resources | dict[str, int] | None = None,
    **params: Any,
) -> Ref:
    """Train a model on ``datasets`` (``config_class``/``task_type`` required, as in the YAML)."""
    return step("train", id=id, inputs={"datasets": list(datasets)}, resources=resources, **params)


def evaluate(model: Ref, *, id: str | None = None, split: str = "validation") -> Ref:
    """Evaluate ``model`` on a split (only ``validation`` lowers today)."""
    return step("evaluate", id=id, inputs={"model": model}, split=split)


def promote(metrics: Ref, *, lifecycle: list[dict[str, Any]], id: str | None = None) -> Ref:
    """Promote through ``lifecycle`` stages (``name``/``metric``/``threshold``/``direction``)."""
    return step("promote", id=id, inputs={"metrics": metrics}, lifecycle=lifecycle)


def hpo(*datasets: Ref, id: str | None = None, **params: Any) -> Ref:
    """A hyper-parameter search step. Known to the IR, **not lowerable yet** (refused at run)."""
    return step("hpo", id=id, inputs={"datasets": list(datasets)}, **params)


def custom_python(entrypoint: str, *upstream: Ref, id: str | None = None) -> Ref:
    """An arbitrary Python entrypoint. Known to the IR, **not lowerable yet** (refused at run)."""
    return step("custom_python", id=id, inputs={"upstream": list(upstream)}, entrypoint=entrypoint)


@dataclass
class PipelineDef:
    """A ``@pipeline``-decorated function. Calling it (or :meth:`compile`) traces it into an IR."""

    fn: Callable[[], Any]
    name: str
    kind: str = "training"
    cluster: str | None = None
    registry: dict[str, Any] = field(default_factory=dict)

    def compile(self) -> dict[str, Any]:
        if _TRACER.get() is not None:
            raise IRError("pipelines cannot be compiled from inside another pipeline's trace")
        tracer = _Tracer(self.name)
        token = _TRACER.set(tracer)
        try:
            self.fn()
        finally:
            _TRACER.reset(token)
        return build_ir(
            name=self.name,
            kind=self.kind,
            nodes=tracer.nodes,
            edges=tracer.edges,
            registry=self.registry,
            target={"cluster": self.cluster} if self.cluster else {},
        )

    __call__ = compile


def pipeline(
    fn: Callable[[], Any] | None = None,
    *,
    name: str | None = None,
    kind: str = "training",
    cluster: str | None = None,
    **registry: Any,
) -> Any:
    """Decorate a zero-argument function as a pipeline definition.

    ``name`` defaults to the function name. ``cluster`` is the run target (a cluster name or
    ``auto``, as ``--cluster``). Any other keyword (``serving``, ``prefect``, ``inference``,
    ``project``, ``enabled``, ``dataplane_bus_uuid``, ``engine``, ``fairness``, ``autoscale``) is
    the matching per-model registry YAML section, carried through unchanged.
    """

    def wrap(f: Callable[[], Any]) -> PipelineDef:
        return PipelineDef(
            fn=f, name=name or f.__name__, kind=kind, cluster=cluster, registry=dict(registry)
        )

    return wrap(fn) if fn is not None else wrap
