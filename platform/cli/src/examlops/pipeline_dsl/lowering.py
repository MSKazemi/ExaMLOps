"""Lower a ``training`` IR onto the existing execution machinery (ADR 0080 decision 3).

The output is the per-model YAML mapping that ``pipelines.model_loader.load_model_yaml`` reads and
``pipelines.pipeline_generator`` registers — the *same structure*, so the run goes through the real
generator, Prefect flow and scheduler abstraction unchanged. Nothing here executes anything.

Anything the lowering cannot represent faithfully is **refused** with :class:`NotLowerableError`,
never skipped: an unlowerable step kind, an unsupported param value, a dataset the train step does
not consume, a second train step, and so on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ir import STEP_KINDS, IRError, topological_order, validate_ir


class NotLowerableError(IRError):
    """A valid IR that the current lowering cannot run. The message names the step and why."""


#: Why each *known* step kind with ``lowerable=False`` is still refused — stated as the concrete
#: thing that is missing, not as "not supported yet". A reader has to be able to tell from the
#: message whether the gap is a lowering bug (fixable here) or an executor nobody has built
#: (fixable only by building it), and these two are firmly the second. Re-checked 2026-09-24.
_NO_EXECUTOR: dict[str, str] = {
    "hpo": (
        "nothing in the platform searches a hyper-parameter space. The only HPO surface is "
        "`exa pipeline hpo start|status|record`, which records a study row and dispatches ONE "
        "baseline training run through the control plane's /retrain; the trials are produced by "
        "an optimiser outside ExaMLOps and reported back with `exa pipeline hpo record`. No "
        "optimiser is a platform dependency (the pinned extra is ray[serve], not ray[tune]; "
        "optuna is in no platform manifest) and `pipelines.pipeline_generator.training_flow` "
        "trains exactly once per run, with no trial loop and no reader of `search_space`. "
        "Lowering this step would mean *building* that search driver, not binding to one"
    ),
    "custom_python": (
        "there is nowhere to put the entrypoint. The per-model registry YAML has no field for "
        "user code (`pipelines.model_loader.ModelYAMLConfig` has no such slot), and "
        "`training_flow` is a fixed task sequence (data_extraction -> data_contract_gate -> "
        "slurm_submit -> slurm_wait -> result_fetch -> evaluate -> log_mlflow -> promote) with "
        "no seam for an extra task. The HPC compute node re-loads the use-case pack, not this "
        "pipeline file, so an entrypoint string would have nothing to resolve against there"
    ),
}


@dataclass
class Lowered:
    """``model_yaml`` is the per-model YAML mapping; ``hints`` are run-time asks the YAML has no
    place for (``cluster``, ``gpus``); ``dropped`` names IR content the YAML cannot carry."""

    model_yaml: dict[str, Any]
    hints: dict[str, Any] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


def _one(nodes: list[dict[str, Any]], kind: str, *, required: bool) -> dict[str, Any] | None:
    found = [n for n in nodes if n["kind"] == kind]
    if len(found) > 1:
        raise NotLowerableError(
            f"the training flow runs exactly one {kind!r} step; the IR has {len(found)} "
            f"({', '.join(n['id'] for n in found)})"
        )
    if not found:
        if required:
            raise NotLowerableError(
                f"the training flow always runs a {kind!r} step; the IR defines none"
            )
        return None
    return found[0]


def lower_training(doc: dict[str, Any]) -> Lowered:
    """Turn a valid ``training`` IR into the registry YAML mapping, or raise."""
    validate_ir(doc)
    nodes = list(doc["nodes"])
    for node in nodes:
        if not STEP_KINDS[node["kind"]].lowerable:
            why = _NO_EXECUTOR.get(
                node["kind"], "no execution path exists for it in the current stack"
            )
            raise NotLowerableError(
                f"step {node['id']!r} ({node['kind']}) is not lowerable yet: {why} "
                "(compile and explain still work; lowering and run refuse it rather than "
                "silently skipping it)"
            )
    train = _one(nodes, "train", required=True)
    assert train is not None
    evaluate = _one(nodes, "evaluate", required=True)
    assert evaluate is not None
    promote = _one(nodes, "promote", required=False)

    edges = doc["edges"]
    fed_in_order = [e["from"] for e in edges if e["to"] == train["id"]]
    consumed = set(fed_in_order)
    topological_order(doc)  # validates the graph once more; the run order is the flow's own
    datasets = [n for n in nodes if n["kind"] == "dataset"]
    orphan = [d["id"] for d in datasets if d["id"] not in consumed]
    if orphan:
        raise NotLowerableError(
            f"dataset step(s) {orphan} are not consumed by the train step; the flow would "
            "silently ignore them"
        )
    if not any(e["from"] == train["id"] and e["to"] == evaluate["id"] for e in edges):
        raise NotLowerableError("the evaluate step must consume the train step's model")
    if promote is not None and not any(
        e["from"] == evaluate["id"] and e["to"] == promote["id"] for e in edges
    ):
        raise NotLowerableError("the promote step must consume the evaluate step's metrics")
    if evaluate["params"].get("split", "validation") != "validation":
        raise NotLowerableError(
            f"step {evaluate['id']!r}: only the 'validation' split is evaluated by the training "
            f"flow, got {evaluate['params']['split']!r}"
        )

    tp = train["params"]
    out: dict[str, Any] = {"name": doc["name"]}
    for key in ("model_class", "config_class", "task_type", "framework"):
        if key in tp:
            out[key] = tp[key]
    if "model" in tp:
        out["model"] = tp["model"]
    ds_out = []
    for did in fed_in_order:  # the author's dataset order — it is the run order
        ds_out.append(dict(next(n for n in nodes if n["id"] == did)["params"]))
    out["datasets"] = ds_out
    if promote is not None:
        out["lifecycle"] = promote["params"]["lifecycle"]
    out.update(doc.get("registry", {}))

    hints: dict[str, Any] = {}
    cluster = (doc.get("target") or {}).get("cluster")
    if cluster:
        hints["cluster"] = cluster
    res = train.get("resources") or {}
    if res.get("gpus"):
        hints["gpus"] = res["gpus"]
    dropped = [
        f"resources of step {n['id']!r}: {n['resources']} (used only as run-time placement hints)"
        for n in nodes
        if n.get("resources") and n["id"] != train["id"]
    ]
    if res:
        dropped.append(
            f"resources of step {train['id']!r}: {res} (the YAML has no field; "
            "`run --ir` passes the GPU count to --cluster placement)"
        )
    if cluster:
        dropped.append(f"target.cluster={cluster!r} (the YAML has no field; `run --ir` uses it)")
    return Lowered(model_yaml=out, hints=hints, dropped=dropped)
