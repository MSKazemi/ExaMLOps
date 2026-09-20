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
    "  exa models sign JPCP 17\n\n"
    "  exa models verify JPCP 17\n\n"
    "  exa models sign JPCP 17 --path ./artifacts/jpcp\n\n"
    "  exa models verify JPCP 17 --path ./artifacts/jpcp\n\n"
    "  exa models bom JPCP 17 --dataset FData --dataset-revision abc123\n\n"
    "  exa models verify JPCP 17 --path ./artifacts/jpcp --mode warn"
)


def _actor() -> str | None:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER")


def _root(path: str) -> Path:
    """The bundle's top directory: relative paths inside it are part of what is signed."""
    target = Path(path)
    return target.parent if target.is_file() else target


def _artifact_paths(path: str) -> list[Path]:
    root = Path(path)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.is_file()]


_PATH_HELP = (
    "Local artifact file or directory. Default: the registered version's artifacts, downloaded "
    "from MLflow exactly as the serving plane downloads them"
)
_KEY_HINT = (
    "Set EXAMLOPS_SIGNING_PRIVATE_KEY_FILE to an Ed25519 PEM key (openssl genpkey -algorithm "
    "ed25519), or the legacy EXAMLOPS_SIGNING_KEY"
)


@app.command("sign", epilog=_EXAMPLES)
def sign(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    version: str = typer.Argument(..., help="Model version"),
    path: str | None = typer.Option(None, "--path", help=_PATH_HELP),
) -> None:
    """Sign a model version's artifacts (Ed25519; HMAC when only the legacy key is set)."""
    from examlops.cli._policy_gate import enforce_and_confirm
    from examlops.supplychain import SigningKeyMissing, sign_model, sign_registered_version

    if not enforce_and_confirm(
        "model_sign",
        {"model": model, "version": version, "path": path, "actor": _actor()},
        what=f"signing {model}@{version}",
        prompt=f"Sign {model}@{version}?",
    ):
        return
    try:
        if path is None:
            result = sign_registered_version(model, version, actor=_actor())
        else:
            paths = _artifact_paths(path)
            if not paths:
                _output.error(f"No artifact files found under {path}")
            result = sign_model(model, version, paths, actor=_actor(), root=_root(path))
    except SigningKeyMissing as exc:
        _output.error(str(exc), hint=_KEY_HINT)
        return  # unreachable — error() raises; satisfies type-checkers
    from examlops.supplychain import signer_public_key

    public = signer_public_key() if result.algo == "ed25519-v2" else None
    if _output.json_mode:
        _output.print_json(
            {
                "model": result.model,
                "version": result.version,
                "digest": result.digest,
                "algo": result.algo,
                "public_key": public,
            }
        )
    _output.ok(f"Signed {model}@{version} ({result.algo}, digest {result.digest[:23]}…)")
    if public:
        _output.hint(f"Serving verifies with: EXAMLOPS_SIGNING_PUBLIC_KEYS={public}")


@app.command("verify", epilog=_EXAMPLES)
def verify(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    path: str | None = typer.Option(None, "--path", help=_PATH_HELP),
    mode: str = typer.Option(
        "enforce", "--mode", help="enforce (exit 1 on failure) or warn (record only)"
    ),
) -> None:
    """Verify a model's signature against current artifact bytes (verify-before-load gate)."""
    import tempfile

    from examlops.supplychain import registered_artifacts, verify_before_load, verify_model

    with tempfile.TemporaryDirectory(prefix="examlops-verify-") as tmp:
        root = registered_artifacts(model, version, Path(tmp)) if path is None else _root(path)
        paths = _artifact_paths(str(root)) if path is None else _artifact_paths(path)
        result = verify_model(model, version, paths, root=root)
        allowed = result.ok or verify_before_load(model, version, paths, mode=mode, root=root)
    if _output.json_mode:
        _output.print_json({"ok": result.ok, "reason": result.reason, "digest": result.digest})
    if result.ok:
        _output.ok(f"{model}@{version} verified ({result.reason})")
        return
    # Failure: honour enforce/warn semantics and set the exit code accordingly.
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
