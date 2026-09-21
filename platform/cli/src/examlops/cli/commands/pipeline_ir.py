"""Implementation behind ``exa pipeline compile | explain | run --ir`` (ADR 0080).

Kept out of ``pipeline.py`` so that module stays a thin Typer surface. Every failure path ends in
``_output.error`` (exit 1); the policy gate is the shared ``_policy_gate.enforce`` pattern, so with
no policy the commands behave exactly as if the gate did not exist.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from examlops.cli import _output
from examlops.cli._policy_gate import enforce
from examlops.pipeline_dsl import (
    STEP_KINDS,
    IRError,
    NotLowerableError,
    lower_training,
    topological_order,
)
from examlops.pipeline_dsl.loader import load_ir, load_pipeline_file


def policy_context(doc: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The facts a ``pipeline_compile`` / ``pipeline_run_ir`` policy rule can match on."""
    nodes = doc["nodes"]
    gpus = sum(int((n.get("resources") or {}).get("gpus", 0)) for n in nodes)
    return {
        "pipeline": doc["name"],
        "kind": doc["kind"],
        "content_hash": doc.get("content_hash", ""),
        "steps": len(nodes),
        "step_kinds": sorted({n["kind"] for n in nodes}),
        "datasets": sorted(n["params"]["name"] for n in nodes if n["kind"] == "dataset"),
        "gpus": gpus,
        "cluster": (doc.get("target") or {}).get("cluster") or "",
        "actor": os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown",
        **extra,
    }


def compile_pipeline(file: str, out: str | None, yaml_path: str | None, untrusted: bool) -> None:
    try:
        pdef, _ = load_pipeline_file(file, sandboxed=untrusted)
        doc = pdef.compile()
    except IRError as exc:
        _output.error(f"Compile failed: {exc}")
    enforce(
        "pipeline_compile",
        policy_context(doc, untrusted=untrusted, source=file),
        what=f"compiling pipeline {doc['name']}",
    )
    lowered_note: list[str] = []
    if yaml_path:
        try:
            lowered = lower_training(doc)
        except NotLowerableError as exc:
            _output.error(f"Not lowerable: {exc}")
        import yaml

        Path(yaml_path).write_text(
            yaml.safe_dump(lowered.model_yaml, sort_keys=False), encoding="utf-8"
        )
        lowered_note = lowered.dropped
    if out:
        Path(out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if _output.json_mode:
        _output.print_json(
            {
                "name": doc["name"],
                "content_hash": doc["content_hash"],
                "ir_file": out,
                "yaml_file": yaml_path,
                "not_carried_by_yaml": lowered_note,
                "ir": doc,
            }
        )
        return
    if not out:
        print(json.dumps(doc, indent=2, sort_keys=True))
    _output.ok(f"Compiled {doc['name']}: {len(doc['nodes'])} steps, {doc['content_hash']}")
    if out:
        _output.info(f"IR written to {out}")
    if yaml_path:
        _output.info(f"Registry YAML written to {yaml_path}")
        for item in lowered_note:
            _output.warning(f"not carried by the YAML: {item}")


def _lowerability(doc: dict[str, Any]) -> tuple[bool, str]:
    try:
        lower_training(doc)
    except IRError as exc:
        return False, str(exc)
    return True, "lowers to the per-model registry YAML"


def explain_pipeline(input_file: str) -> None:
    try:
        doc = load_ir(input_file)
        order = topological_order(doc)
    except IRError as exc:
        _output.error(f"Invalid IR: {exc}")
    by_id = {n["id"]: n for n in doc["nodes"]}
    feeds: dict[str, list[str]] = {i: [] for i in by_id}
    for e in doc["edges"]:
        feeds[e["to"]].append(f"{e['from']}.{e['output']}")
    can, why = _lowerability(doc)
    rows = []
    for pos, nid in enumerate(order, 1):
        n = by_id[nid]
        res = n.get("resources") or {}
        rows.append(
            {
                "order": pos,
                "step": nid,
                "kind": n["kind"],
                "after": ", ".join(sorted(feeds[nid])) or "-",
                "produces": ", ".join(f"{k}:{v}" for k, v in n["outputs"].items()),
                "resources": " ".join(f"{k}={v}" for k, v in sorted(res.items())) or "-",
                "lowerable": STEP_KINDS[n["kind"]].lowerable,
            }
        )
    if _output.json_mode:
        _output.print_json(
            {
                "name": doc["name"],
                "kind": doc["kind"],
                "content_hash": doc.get("content_hash"),
                "target": doc.get("target") or {},
                "runnable": can,
                "runnable_reason": why,
                "plan": rows,
            }
        )
        return
    _output.print_table(
        f"{doc['name']} ({doc['kind']}) {doc.get('content_hash', '')}",
        ["order", "step", "kind", "after", "produces", "resources", "lowerable"],
        [[r[k] for k in r] for r in rows],
    )
    cluster = (doc.get("target") or {}).get("cluster")
    if cluster:
        _output.info(f"target cluster: {cluster}")
    (_output.info if can else _output.warning)(f"run: {why}")


def _scheduler_is_mock() -> bool:
    sched = os.getenv("EXAMLOPS_HPC_SCHEDULER", "").lower().strip()
    if sched:
        return sched == "mock"
    return os.getenv("EXAMLOPS_SLURM_MODE", "mock").lower().strip() != "slurm"


@dataclass
class IRRun:
    name: str
    yaml_path: str
    datasets: list[str]
    hints: dict[str, Any]
    tmpdir: tempfile.TemporaryDirectory


def prepare_ir_run(input_file: str, model: str | None, dataset: str | None) -> IRRun:
    """Validate the IR, consult policy, lower it and write the YAML the generator will register.

    The caller owns ``IRRun.tmpdir`` and must ``cleanup()`` it.
    """
    try:
        doc = load_ir(input_file)
        lowered = lower_training(doc)
    except NotLowerableError as exc:
        _output.error(f"Not lowerable: {exc}")
    except IRError as exc:
        _output.error(f"Invalid IR: {exc}")
    name = doc["name"]
    if model and model != name:
        _output.error(f"--model {model!r} does not match the IR's pipeline name {name!r}.")
    datasets = [d["name"] for d in lowered.model_yaml["datasets"]]
    if dataset and dataset not in datasets:
        _output.error(f"--dataset {dataset!r} is not in the IR (has: {', '.join(datasets)}).")
    enforce(
        "pipeline_run_ir",
        policy_context(doc, source=input_file),
        what=f"running pipeline {name} from IR",
    )
    tmp = tempfile.TemporaryDirectory(prefix="exa_ir_")
    path = Path(tmp.name) / f"{name}.yaml"
    import yaml

    path.write_text(yaml.safe_dump(lowered.model_yaml, sort_keys=False), encoding="utf-8")
    return IRRun(name=name, yaml_path=str(path), datasets=datasets, hints=lowered.hints, tmpdir=tmp)


def refuse_remote_scheduler() -> None:
    """An IR-only model is registered in this process; a remote node would not know it."""
    if not _scheduler_is_mock():
        _output.error(
            "`exa pipeline run --ir` supports the inline (mock) scheduler only: a Slurm/Flux "
            "compute node re-loads models from the pack's models/ directory and would not know "
            "this pipeline.",
            hint="Lower it with `exa pipeline compile FILE --yaml <pack>/models/<name>.yaml`, "
            "review and commit that YAML, then run it by name.",
        )
