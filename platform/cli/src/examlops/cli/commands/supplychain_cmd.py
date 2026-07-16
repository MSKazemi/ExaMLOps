"""D3 — `exa models sign/verify/bom`: ML supply-chain security (ADR 0013)."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Sign, verify, and emit an AI-BOM for model artifacts",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa models sign JPCP 17 --path ./artifacts/jpcp\n\n"
    "  exa models verify JPCP 17 --path ./artifacts/jpcp\n\n"
    "  exa models bom JPCP 17 --dataset FData --dataset-revision abc123\n\n"
    "  exa models verify JPCP 17 --path ./artifacts/jpcp --mode warn"
)


def _actor() -> str | None:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")


def _artifact_paths(path: str) -> list[Path]:
    root = Path(path)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.is_file()]


@app.command("sign", epilog=_EXAMPLES)
def sign(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    version: str = typer.Argument(..., help="Model version"),
    path: str = typer.Option(..., "--path", help="Local artifact file or directory to sign"),
) -> None:
    """Sign a model artifact bundle (HMAC fallback or Sigstore keyless)."""
    from examlops.supplychain import SigningKeyMissing, sign_model

    paths = _artifact_paths(path)
    if not paths:
        _output.error(f"No artifact files found under {path}")
    try:
        result = sign_model(model, version, paths, actor=_actor())
    except SigningKeyMissing as exc:
        _output.error(
            str(exc),
            hint="Set EXAMLOPS_SIGNING_KEY or store secret 'model-signing/key' via exa secrets set",
        )
        return  # unreachable — error() raises; satisfies type-checkers
    if _output.json_mode:
        _output.print_json(
            {
                "model": result.model,
                "version": result.version,
                "digest": result.digest,
                "algo": result.algo,
            }
        )
    _output.ok(f"Signed {model}@{version} ({result.algo}, digest {result.digest[:16]}…)")


@app.command("verify", epilog=_EXAMPLES)
def verify(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    path: str = typer.Option(..., "--path", help="Local artifact file or directory to verify"),
    mode: str = typer.Option(
        "enforce", "--mode", help="enforce (exit 1 on failure) or warn (record only)"
    ),
) -> None:
    """Verify a model's signature against current artifact bytes (verify-before-load gate)."""
    from examlops.supplychain import verify_before_load, verify_model

    paths = _artifact_paths(path)
    result = verify_model(model, version, paths)
    if _output.json_mode:
        _output.print_json({"ok": result.ok, "reason": result.reason, "digest": result.digest})
    if result.ok:
        _output.ok(f"{model}@{version} verified ({result.reason})")
        return
    # Failure: honour enforce/warn semantics and set the exit code accordingly.
    allowed = verify_before_load(model, version, paths, mode=mode)
    if mode == "warn" and allowed:
        _output.warning(
            f"{model}@{version} FAILED verification: {result.reason} (warn: not blocking)"
        )
        return
    _output.error(f"{model}@{version} FAILED verification: {result.reason}")


@app.command("bom", epilog=_EXAMPLES)
def bom(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    dataset: str | None = typer.Option(None, "--dataset", help="Training dataset name"),
    dataset_revision: str | None = typer.Option(
        None, "--dataset-revision", help="Pinned dataset revision (A1)"
    ),
    framework: str | None = typer.Option(None, "--framework", help="ML framework"),
    output: str | None = typer.Option(None, "--output", help="Write BOM JSON to this file"),
) -> None:
    """Generate a CycloneDX AI-BOM for a model version."""
    import json

    from examlops.supplychain import generate_ai_bom

    doc = generate_ai_bom(
        model,
        version,
        dataset=dataset,
        dataset_revision=dataset_revision,
        framework=framework,
    )
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2))
        _output.ok(f"AI-BOM written to {out}")
        return
    if _output.json_mode:
        _output.print_json(doc)
    _output.ok(f"AI-BOM generated for {model}@{version}")
