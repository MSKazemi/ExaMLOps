"""The pipeline intermediate representation (ADR 0080).

A pipeline is a small typed DAG of *steps* (nodes) joined by typed *edges*. The IR is a plain,
JSON-serialisable dict with a ``schema_version`` and a canonical content hash, so the same pipeline
always serialises, validates and hashes identically. This module is stdlib-only and has no
knowledge of how a pipeline runs — that is :mod:`examlops.pipeline_dsl.lowering`'s job, and the
existing Prefect + scheduler stack's after that.

The IR is deliberately a *superset* of the per-model registry YAML: it carries the dependency graph,
resource asks and target that the YAML has no place for. For the ``training`` kind the lowering
produces exactly the per-model YAML mapping ``pipelines.model_loader`` already consumes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

#: Pipeline kinds the IR accepts. ``inference`` is reserved (ADR 0010's pipeline is not authored
#: through this DSL) and refused, not ignored.
PIPELINE_KINDS = frozenset({"training"})

#: Top-level keys of the ``registry`` block — the per-model YAML sections a graph has no node for.
REGISTRY_KEYS = frozenset(
    {
        "serving",
        "prefect",
        "inference",
        "project",
        "enabled",
        "dataplane_bus_uuid",
        "engine",
        "fairness",
        "autoscale",
    }
)

_RESOURCE_KEYS = ("gpus", "cpus", "nodes")


class IRError(ValueError):
    """The IR (or a DSL-built pipeline) is invalid. The message names the offending part."""


@dataclass(frozen=True)
class Port:
    """One typed input or output slot of a step kind. ``many`` inputs accept several edges."""

    type: str
    many: bool = False
    required: bool = True


@dataclass(frozen=True)
class StepKind:
    """The static contract of a step kind: its ports, its allowed params, whether it lowers."""

    name: str
    inputs: dict[str, Port]
    outputs: dict[str, str]
    params: frozenset[str]
    required_params: frozenset[str] = frozenset()
    #: ``True`` when :mod:`examlops.pipeline_dsl.lowering` can turn it into the existing machinery.
    lowerable: bool = True


#: The closed set of step kinds. An unknown kind is an error at validation time; a *known* kind
#: with ``lowerable=False`` validates and explains but is refused at compile ``--yaml`` / run time.
STEP_KINDS: dict[str, StepKind] = {
    "dataset": StepKind(
        "dataset",
        inputs={},
        outputs={"data": "dataset"},
        params=frozenset(
            {
                "name",
                "backend",
                "cache_dir",
                "batch_size",
                "columns",
                "input_features",
                "output_features",
                "splits",
                "dataplane",
            }
        ),
        required_params=frozenset({"name"}),
    ),
    "train": StepKind(
        "train",
        inputs={"datasets": Port("dataset", many=True)},
        outputs={"model": "model"},
        params=frozenset({"model_class", "config_class", "task_type", "framework", "model"}),
        required_params=frozenset({"config_class", "task_type"}),
    ),
    "evaluate": StepKind(
        "evaluate",
        inputs={"model": Port("model")},
        outputs={"metrics": "metrics"},
        params=frozenset({"split"}),
    ),
    "promote": StepKind(
        "promote",
        inputs={"metrics": Port("metrics")},
        outputs={"alias": "alias"},
        params=frozenset({"lifecycle"}),
        required_params=frozenset({"lifecycle"}),
    ),
    # Known kinds with no lowering yet: they validate, compile and explain, and are refused
    # (never skipped) when a run or a YAML lowering is requested.
    "hpo": StepKind(
        "hpo",
        inputs={"datasets": Port("dataset", many=True)},
        outputs={"model": "model"},
        params=frozenset({"config_class", "search_space", "trials"}),
        lowerable=False,
    ),
    "custom_python": StepKind(
        "custom_python",
        inputs={"upstream": Port("any", many=True, required=False)},
        outputs={"result": "any"},
        params=frozenset({"entrypoint"}),
        required_params=frozenset({"entrypoint"}),
        lowerable=False,
    ),
}


def _canon(value: Any, where: str) -> Any:
    """Round-trip through strict JSON so tuples become lists and non-JSON values are refused."""
    try:
        return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise IRError(f"{where} is not JSON-serialisable: {exc}") from exc


def canonical_json(doc: dict[str, Any]) -> str:
    """Canonical serialisation: sorted keys, no whitespace, ASCII — the input to the hash."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def content_hash(doc: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``doc`` excluding its own ``content_hash``."""
    body = {k: v for k, v in doc.items() if k != "content_hash"}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def new_node(
    node_id: str,
    kind: str,
    params: dict[str, Any] | None = None,
    resources: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build one node dict with its typed ports filled from the kind's contract."""
    spec = STEP_KINDS.get(kind)
    if spec is None:
        raise IRError(f"unknown step kind {kind!r} (known: {', '.join(sorted(STEP_KINDS))})")
    return {
        "id": node_id,
        "kind": kind,
        "params": _canon(params or {}, f"params of step {node_id!r}"),
        "inputs": {p: port.type + ("[]" if port.many else "") for p, port in spec.inputs.items()},
        "outputs": dict(spec.outputs),
        "resources": _canon(resources or {}, f"resources of step {node_id!r}"),
    }


def build_ir(
    *,
    name: str,
    kind: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    registry: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble, validate and hash an IR document. Raises :class:`IRError` if it is invalid."""
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "name": name,
        # Declaration order is kept, not sorted: the order of the edges into a ``many`` port (the
        # train step's datasets) is meaningful, and the hash is over exactly what was authored.
        "nodes": list(nodes),
        "edges": list(edges),
        "registry": _canon(registry or {}, "registry"),
        "target": _canon(target or {}, "target"),
    }
    validate_ir(doc)
    doc["content_hash"] = content_hash(doc)
    return doc


def _fail(msg: str) -> IRError:
    return IRError(msg)


def validate_ir(doc: Any) -> None:
    """Strictly validate an IR document; raise :class:`IRError` naming the first problem.

    Checks: schema version, pipeline kind, unique node ids, known step kinds, param/resource
    shape, node ports equal to the kind's contract, edges that resolve to real typed ports,
    required inputs connected, a single-consumer rule for non-``many`` inputs, no cycles, and —
    when present — that ``content_hash`` matches the content.
    """
    if not isinstance(doc, dict):
        raise _fail("IR must be a JSON object")
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise _fail(f"unsupported schema_version {version!r} (this build reads {SCHEMA_VERSION})")
    if doc.get("kind") not in PIPELINE_KINDS:
        raise _fail(
            f"unknown pipeline kind {doc.get('kind')!r} (known: {', '.join(sorted(PIPELINE_KINDS))})"
        )
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _fail("pipeline 'name' must be a non-empty string")
    nodes, edges = doc.get("nodes"), doc.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise _fail("IR needs a non-empty 'nodes' list")
    if not isinstance(edges, list):
        raise _fail("IR 'edges' must be a list")

    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise _fail("every node must be an object")
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            raise _fail("every node needs a non-empty string 'id'")
        if nid in by_id:
            raise _fail(f"duplicate step id {nid!r}")
        by_id[nid] = node
        _validate_node(node)

    registry = doc.get("registry", {})
    if not isinstance(registry, dict):
        raise _fail("'registry' must be an object")
    unknown = sorted(set(registry) - REGISTRY_KEYS)
    if unknown:
        raise _fail(
            f"unknown registry key(s) {unknown} (allowed: {', '.join(sorted(REGISTRY_KEYS))})"
        )
    target = doc.get("target", {})
    if not isinstance(target, dict) or set(target) - {"cluster"}:
        raise _fail("'target' may only carry 'cluster'")
    if target.get("cluster") is not None and not isinstance(target["cluster"], str):
        raise _fail("target.cluster must be a string (a cluster name or 'auto')")

    fed: dict[tuple[str, str], int] = {}
    adjacency: dict[str, set[str]] = {nid: set() for nid in by_id}
    seen_edges: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {"from", "output", "to", "input"}:
            raise _fail("every edge needs exactly: from, output, to, input")
        src, out, dst, inp = edge["from"], edge["output"], edge["to"], edge["input"]
        key = (src, out, dst, inp)
        if key in seen_edges:
            raise _fail(f"duplicate edge {src}.{out} -> {dst}.{inp}")
        seen_edges.add(key)
        if src not in by_id:
            raise _fail(f"dangling edge: source step {src!r} does not exist")
        if dst not in by_id:
            raise _fail(f"dangling edge: target step {dst!r} does not exist")
        src_kind, dst_kind = STEP_KINDS[by_id[src]["kind"]], STEP_KINDS[by_id[dst]["kind"]]
        if out not in src_kind.outputs:
            raise _fail(f"dangling edge: step {src!r} ({src_kind.name}) has no output {out!r}")
        port = dst_kind.inputs.get(inp)
        if port is None:
            raise _fail(f"dangling edge: step {dst!r} ({dst_kind.name}) has no input {inp!r}")
        produced = src_kind.outputs[out]
        if port.type != "any" and produced != "any" and produced != port.type:
            raise _fail(
                f"type mismatch on {src}.{out} -> {dst}.{inp}: {produced} is not {port.type}"
            )
        fed[(dst, inp)] = fed.get((dst, inp), 0) + 1
        adjacency[src].add(dst)

    for nid, node in by_id.items():
        spec = STEP_KINDS[node["kind"]]
        for pname, port in spec.inputs.items():
            count = fed.get((nid, pname), 0)
            if port.required and count == 0:
                raise _fail(
                    f"step {nid!r} ({spec.name}): required input {pname!r} is not connected"
                )
            if not port.many and count > 1:
                raise _fail(
                    f"step {nid!r} ({spec.name}): input {pname!r} takes one edge, got {count}"
                )

    _reject_cycles(adjacency)

    claimed = doc.get("content_hash")
    if claimed is not None and claimed != content_hash(doc):
        raise _fail("content_hash does not match the IR content (edited after compile?)")


def _validate_node(node: dict[str, Any]) -> None:
    nid, kind = node["id"], node.get("kind")
    spec = STEP_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise _fail(
            f"step {nid!r}: unknown step kind {kind!r} (known: {', '.join(sorted(STEP_KINDS))})"
        )
    params = node.get("params", {})
    if not isinstance(params, dict):
        raise _fail(f"step {nid!r}: params must be an object")
    bad = sorted(set(params) - spec.params)
    if bad:
        raise _fail(
            f"step {nid!r} ({kind}): unknown param(s) {bad} (allowed: {', '.join(sorted(spec.params))})"
        )
    missing = sorted(spec.required_params - set(params))
    if missing:
        raise _fail(f"step {nid!r} ({kind}): missing required param(s) {missing}")
    res = node.get("resources", {})
    if not isinstance(res, dict) or set(res) - set(_RESOURCE_KEYS):
        raise _fail(f"step {nid!r}: resources may only carry {list(_RESOURCE_KEYS)}")
    for key, val in res.items():
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise _fail(f"step {nid!r}: resources.{key} must be a non-negative integer")
    expect_in = {p: port.type + ("[]" if port.many else "") for p, port in spec.inputs.items()}
    if node.get("inputs", {}) != expect_in or node.get("outputs", {}) != dict(spec.outputs):
        raise _fail(f"step {nid!r} ({kind}): typed ports differ from the {kind} contract")
    try:
        json.dumps(params, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail(f"step {nid!r}: params are not JSON-serialisable: {exc}") from exc


def _reject_cycles(adjacency: dict[str, set[str]]) -> None:
    """Kahn's algorithm; on failure name the steps left on the cycle."""
    indeg = {n: 0 for n in adjacency}
    for targets in adjacency.values():
        for t in targets:
            indeg[t] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    seen = 0
    while ready:
        n = ready.pop(0)
        seen += 1
        for t in sorted(adjacency[n]):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
    if seen != len(adjacency):
        stuck = sorted(n for n, d in indeg.items() if d > 0)
        raise _fail(f"cycle detected among steps: {', '.join(stuck)}")


def topological_order(doc: dict[str, Any]) -> list[str]:
    """Deterministic topological order of step ids (ties broken alphabetically). Validates first."""
    validate_ir(doc)
    adjacency: dict[str, set[str]] = {n["id"]: set() for n in doc["nodes"]}
    for e in doc["edges"]:
        adjacency[e["from"]].add(e["to"])
    indeg = {n: 0 for n in adjacency}
    for targets in adjacency.values():
        for t in targets:
            indeg[t] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for t in sorted(adjacency[n]):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
        ready.sort()
    return order
