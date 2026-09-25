"""E2 — `exa models quantize` + `exa models engine`: optimized inference engines (ADR 0016)."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Inference-engine config, validation, and quantization",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa models quantize JPCP 17 --method awq --path ./artifacts/jpcp\n\n"
    "  exa models engine validate ./usecases/reference/models/jpcp.yaml\n\n"
    "  exa models engine list"
)

_EXAMPLES_PARITY = (
    "Examples:\n\n"
    "  exa models parity JPCP 17-awq\n\n"
    "  exa --json models parity JPCP 17-awq\n\n"
    "  exa models parity JPCP 17-awq --tolerance 1e-4\n\n"
    "Declare the per-model tolerance as 'parity_tolerance' in the model's YAML — a ranking\n"
    "model tolerates far more numeric drift than one whose output is a physical quantity."
)


def _actor() -> str | None:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")


@app.command("quantize", epilog=_EXAMPLES)
def quantize(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    version: str = typer.Argument(..., help="Base model version to quantize"),
    method: str = typer.Option("awq", "--method", help="awq | gptq | fp8 | int8"),
    path: str | None = typer.Option(
        None, "--path", help="Local artifact dir to sign for the new version (D3)"
    ),
    dataset: str | None = typer.Option(None, "--dataset", help="Training dataset (for BOM)"),
    dataset_revision: str | None = typer.Option(
        None, "--dataset-revision", help="Pinned dataset revision (for BOM)"
    ),
) -> None:
    """Quantize a model → register a new signed + BOM'd version (GWT-3)."""
    from examlops.engines import quantize_model

    artifact_paths = None
    if path:
        root = Path(path)
        artifact_paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    try:
        new_version = quantize_model(
            model,
            version,
            method,
            artifact_paths=artifact_paths,
            dataset=dataset,
            dataset_revision=dataset_revision,
            actor=_actor(),
        )
    except ValueError as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json(
            {"model": model, "base": version, "new_version": new_version, "method": method}
        )
    _output.ok(f"Quantized {model}@{version} → {new_version} ({method}); signed + BOM'd (D3)")


engine_app = typer.Typer(
    help="Inspect and validate per-model engine config",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(engine_app, name="engine")


@engine_app.command("list")
def engine_list() -> None:
    """List available inference engines."""
    from examlops.engines import _ENGINES

    rows = [[name, cls.__name__] for name, cls in _ENGINES.items()]
    if _output.json_mode:
        _output.print_json([{"engine": n, "class": c} for n, c in rows])
        return
    _output.print_table("Inference Engines", ["Engine", "Class"], rows)


@engine_app.command("validate")
def engine_validate(
    yaml_path: str = typer.Argument(..., help="Path to a per-model YAML with an engine: block"),
) -> None:
    """Validate a model YAML's engine block (the CI integrity guard uses the same check)."""
    import yaml

    from examlops.engines import validate_engine_block

    doc = yaml.safe_load(Path(yaml_path).read_text()) or {}
    block = doc.get("engine")
    if block is None:
        _output.ok(f"{yaml_path}: no engine block (defaults apply) — ok")
        return
    errors = validate_engine_block(block)
    if errors:
        for e in errors:
            _output.warning(e)
        _output.error(f"{yaml_path}: engine block invalid ({len(errors)} error(s))")
        return
    _output.ok(f"{yaml_path}: engine block valid")


_EXAMPLES_QGATE = (
    "Examples:\n\n"
    "  exa models quantize-gate JPCP 17-awq\n\n"
    "  exa --json models quantize-gate JPCP 17-awq\n\n"
    "Needs a C3 gate (exa eval gate set) and suite scores for BOTH the base version and the\n"
    "quantized one (exa eval run). Exits 1 when the gate refuses — usable as a CI step."
)


@app.command("quantize-gate", epilog=_EXAMPLES_QGATE)
def quantize_gate(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    version: str = typer.Argument(..., help="Quantized version, e.g. 17-awq"),
) -> None:
    """Quality-retention gate: a quantized version vs its base version (ADR 0016, C3).

    Mandatory for promotion: `exa pipeline promote` and the training-flow promotion refuse a
    quantized version that has not passed it. No configured C3 gate, or missing scores on either
    side, is a refusal — never a pass. The verdict is recorded in gate_reports and audited.
    """
    from examlops.engines.quality import quantization_quality_gate

    result = quantization_quality_gate(model, version, actor=_actor())
    if result is None:
        _output.ok(f"{model} v{version} is not a quantized version — the gate does not apply.")
        return
    if _output.json_mode:
        _output.print_json(result.as_dict())
    else:
        _output.print_record(
            {
                "Verdict": "PASSED" if result.passed else "REFUSED",
                "Base version": result.base_version,
                "Method": result.method,
                "Suite": result.suite or "-",
                "Failing": ", ".join(result.failing) or "-",
            }
        )
        if result.passed:
            _output.ok(result.reason)
        else:
            _output.warning(result.reason)
    if not result.passed:
        raise typer.Exit(1)


@app.command("parity", epilog=_EXAMPLES_PARITY)
def parity(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    target_version: str = typer.Argument(..., help="Quantized version, e.g. 17-awq"),
    tolerance: float | None = typer.Option(
        None, "--tolerance", help="Override the model's declared parity_tolerance"
    ),
) -> None:
    """Portability gate: compare a quantized version against its base (ADR 0117).

    Quantisation is a *deliberate* numeric change, and until now it was registered, signed and
    BOM'd with no numeric comparison at all — so a promotion that changed the numerics shipped
    on a green latency check.

    Three verdicts, and only one lets an autonomous promotion through. ``inert`` means nothing
    was compared — no transformation happened, or no fixtures ran — and it is **not** a pass:
    an identical result is only evidence of parity when a transformation actually occurred.
    """
    from examlops.parity import run_quantization_parity_gate

    result = run_quantization_parity_gate(model, target_version, actor=_actor())
    if result is None:
        _output.ok(
            f"{model} v{target_version} does not change the execution target — "
            "the portability gate does not apply."
        )
        return
    if _output.json_mode:
        _output.print_json(result.as_dict())
        return
    _output.print_table(
        f"Portability gate — {model}",
        ["Field", "Value"],
        [
            ["Verdict", result.verdict.upper()],
            ["Source version", result.source_version],
            ["Target version", result.target_version],
            ["Weights transformed", "yes" if result.transformed else "NO"],
            ["Fixtures compared", str(result.n_fixtures)],
            [
                "Max abs divergence",
                "—" if result.max_abs_divergence is None else f"{result.max_abs_divergence:.6g}",
            ],
            [
                "Max rel divergence",
                "—" if result.max_rel_divergence is None else f"{result.max_rel_divergence:.6g}",
            ],
            ["Tolerance", "—" if result.tolerance is None else f"{result.tolerance:.6g}"],
            [
                "Permits autonomous promotion",
                "yes" if result.permits_autonomous_promotion else "NO",
            ],
        ],
    )
    _output.detail(result.reason)
    if result.verdict == "inert":
        _output.hint(
            "'inert' is not a pass — nothing was compared. On a host without CUDA, "
            "quantize_model() records provenance only and the weights are unchanged, so an "
            "identical result would prove nothing."
        )
