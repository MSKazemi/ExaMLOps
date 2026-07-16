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
    "  exa models engine validate ./pipelines/models/jpcp.yaml\n\n"
    "  exa models engine list"
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
