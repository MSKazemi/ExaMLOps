"""Implementation behind ``exa pipeline compile | decompile | explain | show | run --ir`` (ADR 0080).

Kept out of ``pipeline.py`` so that module stays a thin Typer surface. Every failure path ends in
``_output.error`` (exit 1); the policy gate is the shared ``_policy_gate.enforce`` pattern, so with
no policy the commands behave exactly as if the gate did not exist.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
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
from examlops.pipeline_dsl.loader import (
    MAX_IR_BYTES,
    is_yaml_ir,
    load_ir,
    load_pipeline_file,
    safe_load_yaml,
)


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
    # ADR 0080 decision 2: `-o model.yaml` writes the registry YAML — the IR in the ADR's own
    # terms. `-o x.json` (or any other suffix) keeps writing the JSON graph.
    out_is_yaml = bool(out) and is_yaml_ir(str(out))
    yaml_targets = [p for p in (yaml_path, out if out_is_yaml else None) if p]
    if yaml_targets:
        try:
            lowered = lower_training(doc)
        except NotLowerableError as exc:
            _output.error(f"Not lowerable: {exc}")
        import yaml

        text = yaml.safe_dump(lowered.model_yaml, sort_keys=False)
        for target in yaml_targets:
            Path(target).write_text(text, encoding="utf-8")
        lowered_note = lowered.dropped
    if out and not out_is_yaml:
        Path(out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if _output.json_mode:
        _output.print_json(
            {
                "name": doc["name"],
                "content_hash": doc["content_hash"],
                "ir_file": out,
                "ir_format": ("yaml" if out_is_yaml else "json") if out else None,
                "yaml_file": yaml_path or (out if out_is_yaml else None),
                "not_carried_by_yaml": lowered_note,
                "ir": doc,
            }
        )
        return
    if not out:
        print(json.dumps(doc, indent=2, sort_keys=True))
    _output.ok(f"Compiled {doc['name']}: {len(doc['nodes'])} steps, {doc['content_hash']}")
    if out:
        _output.info(f"IR ({'registry YAML' if out_is_yaml else 'JSON graph'}) written to {out}")
    if yaml_path:
        _output.info(f"Registry YAML written to {yaml_path}")
    for item in lowered_note:
        _output.warning(f"not carried by the YAML: {item}")


def decompile_model(model_yaml: str, out: str | None, force: bool) -> None:
    """The reverse of ``compile --yaml``: a registry YAML back into ``@pipeline`` DSL source.

    Refuses (exit 1) rather than writing a file whenever the YAML holds anything the DSL cannot
    express — the point of the command is a twin, and a twin that quietly drops a section is worse
    than no file at all.
    """
    from examlops.pipeline_dsl.decompile import ir_from_model_yaml, render_pipeline_source

    path = Path(model_yaml)
    if not path.is_file():
        _output.error(f"Model YAML not found: {path}")
    _, raw = read_model_yaml(path)
    try:
        doc = ir_from_model_yaml(raw)
        source = render_pipeline_source(doc, source=str(path))
    except IRError as exc:
        _output.error(
            f"Not representable as a pipeline: {exc}",
            hint="Keep authoring this model in YAML; the DSL is additive, not a replacement.",
        )
    if out:
        target = Path(out)
        if target.exists() and not force:
            _output.error(f"{target} already exists.", hint="Re-run with --force to overwrite it.")
        target.write_text(source, encoding="utf-8")
    if _output.json_mode:
        _output.print_json(
            {
                "name": doc["name"],
                "content_hash": doc["content_hash"],
                "model_yaml": str(path),
                "flow_file": out,
                "steps": [{"id": n["id"], "kind": n["kind"]} for n in doc["nodes"]],
                "source": source,
            }
        )
        return
    if not out:
        print(source, end="")
    _output.ok(f"Decompiled {doc['name']}: {len(doc['nodes'])} steps, {doc['content_hash']}")
    if out:
        _output.info(f"Pipeline source written to {out}")
        _output.hint(f"Round-trip it: exa pipeline compile {out} --yaml {path}")


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
    #: The lowered YAML's ``placement:`` section — the train step's full ask (gpus, cpus, nodes)
    #: and target cluster. ``hints`` only repeats cluster/gpus; this is what placement scores.
    placement: dict[str, Any] = field(default_factory=dict)


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
    return IRRun(
        name=name,
        yaml_path=str(path),
        datasets=datasets,
        hints=lowered.hints,
        tmpdir=tmp,
        placement=dict(lowered.model_yaml.get("placement") or {}),
    )


def note_remote_staging() -> None:
    """Say where an IR-only model's definition goes when the scheduler is Slurm/Flux.

    ``run --ir`` used to refuse a remote scheduler because the compute node re-loads models from
    the pack's ``models/`` directory and would not know an IR-only pipeline. The generator now
    stages the lowered YAML into the job's working directory through the scheduler adapter's own
    transport and the node registers it from there (``slurm_train_script.py --model-yaml``), so
    the run is portable across mock/Slurm/Flux like a YAML-authored model (ADR 0080 decision 3).
    The ``config_class`` shim still has to exist in the pack the node loads.
    """
    if not _scheduler_is_mock():
        _output.info(
            "remote scheduler: the lowered pipeline YAML is staged to the job's working "
            "directory and registered on the compute node (its config_class shim must exist "
            "in the node's use-case pack)."
        )


# ── `exa pipeline show NAME [--ir]` ────────────────────────────────────────────────────────────


def find_model_yaml(name: str, models_dir: Path | None = None) -> Path | None:
    """The active pack's per-model YAML whose ``name:`` is ``name`` (case-insensitive), or None.

    Matches on the declared ``name`` (the registry key), falling back to the file stem, so
    ``JPCP`` finds ``jpcp.yaml``. Files starting with ``_`` are skipped, as the loader does.

    A file that is oversize or does not parse cannot be matched by its declared name, but it is
    still returned on a *stem* match, so the caller reports why the model's YAML is unusable
    (:func:`read_model_yaml`) instead of acting as if the model had no YAML at all.
    """
    import yaml

    if models_dir is None:
        from examlops.usecase import models_dir as _models_dir

        models_dir = _models_dir()
    if not models_dir.is_dir():
        return None
    want = name.strip().lower()
    stem_match: Path | None = None
    for path in sorted(models_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        if path.stem.lower() == want:
            stem_match = path
        try:
            if path.stat().st_size > MAX_IR_BYTES:
                continue
            raw = safe_load_yaml(path.read_text(encoding="utf-8"), path.name) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError, IRError):
            continue
        declared = raw.get("name") if isinstance(raw, dict) else None
        if isinstance(declared, str) and declared.lower() == want:
            return path
    return stem_match


def read_model_yaml(path: Path) -> tuple[str, Any]:
    """``(text, parsed)`` of a pack model YAML, or ``_output.error`` (exit 1) — never a default.

    A YAML that exists but cannot be read is an operator-visible fault: treating it as "no YAML"
    would silently drop its ``placement:`` section (fail open) or misreport the model as unknown.
    """
    import yaml

    try:
        size = path.stat().st_size
        if size > MAX_IR_BYTES:
            _output.error(f"{path.name}: {size} bytes exceeds the {MAX_IR_BYTES}-byte IR size cap")
        text = path.read_text(encoding="utf-8")
        return text, safe_load_yaml(text, path.name)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, IRError) as exc:
        _output.error(f"{path.name}: could not be read as YAML: {exc}")
    raise AssertionError("unreachable")  # pragma: no cover - _output.error exits


def show_model(name: str, as_ir: bool) -> None:
    """Print a pack model's pipeline IR: its registry YAML, or with ``--ir`` the JSON graph.

    Read-only and name-addressed (no filesystem argument), so it is safe from the dashboard.
    """
    from examlops.pipeline_dsl.decompile import ir_from_model_yaml

    path = find_model_yaml(name)
    if path is None:
        _output.error(
            f"No model named {name!r} in the active use-case pack.",
            hint="`exa pipeline list` shows the models; EXAMLOPS_USECASE_DIR selects the pack.",
        )
    text, raw = read_model_yaml(path)
    doc: dict[str, Any] | None = None
    why = ""
    try:
        doc = ir_from_model_yaml(raw)
    except IRError as exc:
        why = str(exc)
    if as_ir and doc is None:
        _output.error(
            f"{path.name} has no pipeline-IR form: {why}",
            hint="Without --ir the registry YAML itself is shown; it stays first-class.",
        )
    if _output.json_mode:
        _output.print_json(
            {
                "name": raw.get("name", name) if isinstance(raw, dict) else name,
                "file": path.name,
                "format": "ir" if as_ir else "yaml",
                "content_hash": doc["content_hash"] if doc else None,
                "representable": doc is not None,
                "not_representable_reason": why or None,
                "model_yaml": raw,
                **({"ir": doc} if as_ir else {}),
            }
        )
        return
    if as_ir:
        assert doc is not None
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    if doc is not None:
        _output.info(f"{path.name}: {len(doc['nodes'])} steps, {doc['content_hash']}")
    else:
        _output.warning(f"{path.name} has no pipeline-IR form: {why}")


def placement_for_model(name: str) -> dict[str, Any]:
    """The pack model's valid ``placement:`` section, or ``{}`` when the model has none.

    Fail-closed: an unreadable/unparseable model YAML or an invalid section exits 1. Only a model
    that is not in the pack at all, or whose YAML has no section, yields ``{}``.
    """
    from examlops.pipeline_dsl.placement import validate_placement_block

    path = find_model_yaml(name)
    if path is None:
        return {}
    _, raw = read_model_yaml(path)
    block = raw.get("placement") if isinstance(raw, dict) else None
    if block is None:
        return {}
    problems = validate_placement_block(block)
    if problems:
        _output.error(f"{path.name}: invalid placement section: {'; '.join(problems)}")
    return dict(block)
