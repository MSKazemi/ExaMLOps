"""A8 — `exa reproduce`: signed reproducibility bundles (ADR 0038).

Capture a signed manifest of every input to a model version, rebuild + metric-verify within
a documented tolerance, and detect when a bundle is no longer reproducible. Bundles feed D1
technical docs and D2 evidence. No bit-exactness is claimed (GPU non-determinism).
"""

from __future__ import annotations

import json
import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Reproducibility bundles — signed manifest + rebuild/verify (A8)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa reproduce build JPCP 17 --dataset PM100 --revision <rev>\n\n"
    "  exa reproduce run JPCP 17\n\n"
    "  exa reproduce run JPCP 17 --observed '{\"rmse\": 4.9}'\n\n"
    "  exa reproduce verify JPCP 17\n\n"
    "  exa reproduce list"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("build", epilog=_EXAMPLES)
def build(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    dataset: str = typer.Option(None, "--dataset", help="Dataset name"),
    revision: str = typer.Option(None, "--revision", help="A1 dataset revision"),
    seed: int = typer.Option(None, "--seed", help="RNG seed to record"),
    hyperparams: str = typer.Option(None, "--hyperparams", help="JSON hyperparameters"),
    metrics: str = typer.Option(None, "--metrics", help="JSON recorded metrics"),
    image_digest: str = typer.Option(None, "--image-digest", help="Container image digest"),
) -> None:
    """Capture + sign a reproducibility bundle for a model version (R1/R2)."""
    from examlops.reproducibility import build_bundle

    hp = json.loads(hyperparams) if hyperparams else None
    mt = json.loads(metrics) if metrics else None
    bundle = build_bundle(
        model,
        version,
        dataset_name=dataset,
        dataset_revision=revision,
        hyperparams=hp,
        metrics=mt,
        seeds={"global": seed} if seed is not None else None,
        image_digest=image_digest,
        actor=_actor(),
    )
    if _output.json_mode:
        _output.print_json(
            {
                "model": bundle.model,
                "version": bundle.version,
                "bundle_version": bundle.bundle_version,
                "manifest_hash": bundle.manifest_hash,
                "signed": bundle.signed,
            }
        )
        return
    _output.ok(
        f"Built bundle v{bundle.bundle_version} for {model}/{version} "
        f"(hash {bundle.manifest_hash[:16]}…)"
    )
    if not bundle.signed:
        _output.warning("  unsigned — no signing key (set EXAMLOPS_SIGNING_KEY to sign).")


@app.command("run")
def run(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    observed: str = typer.Option(
        None, "--observed", help="JSON of re-observed metrics to match against recorded"
    ),
) -> None:
    """Rebuild plan + metric-match within tolerance — never claims bit-exactness (R3/GWT-2)."""
    from examlops.reproducibility import reproduce

    obs = json.loads(observed) if observed else None
    result = reproduce(model, version, observed_metrics=obs, actor=_actor())
    if _output.json_mode:
        _output.print_json(
            {
                "model": result.model,
                "version": result.version,
                "reproducible": result.verify.reproducible,
                "metric_match": result.metric_match,
                "bit_exact": result.bit_exact,
                "details": result.details,
                "problems": result.verify.problems,
            }
        )
        return
    if not result.verify.reproducible:
        _output.warning(f"Bundle not reproducible: {'; '.join(result.verify.problems)}")
    for d in result.details:
        _output.info(f"  · {d}")
    if result.metric_match is True:
        _output.ok("Metrics match recorded values within tolerance (not bit-exact).")
    elif result.metric_match is False:
        _output.error("Metrics DO NOT match within tolerance.")
    else:
        _output.info("No re-observed metrics supplied — plan + input verification only.")


@app.command("verify")
def verify(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
) -> None:
    """Check referenced inputs still exist + hashes match (R5/GWT-4). Exit 1 if rotted."""
    from examlops.reproducibility import verify_bundle

    result = verify_bundle(model, version)
    if _output.json_mode:
        _output.print_json(
            {
                "model": result.model,
                "version": result.version,
                "reproducible": result.reproducible,
                "inputs": result.inputs,
                "problems": result.problems,
            }
        )
        raise typer.Exit(0 if result.reproducible else 1)
    if result.reproducible:
        _output.ok(f"{model}/{version} is reproducible — all inputs present + hashes match.")
    else:
        _output.error(f"{model}/{version} NON-reproducible: {'; '.join(result.problems)}")
    if result.inputs:
        _output.print_table(
            "Input Verification",
            ["Kind", "Ref", "OK", "Reason"],
            [
                [i["kind"], str(i.get("ref") or "")[:40], "✓" if i["ok"] else "✗", i["reason"]]
                for i in result.inputs
            ],
        )
    raise typer.Exit(0 if result.reproducible else 1)


@app.command("list")
def list_cmd(
    model: str = typer.Argument(None, help="Filter by model"),
) -> None:
    """List reproducibility bundles."""
    from examlops.platform_db import list_repro_bundles

    bundles = list_repro_bundles(model)
    if _output.json_mode:
        _output.print_json(bundles)
        return
    if not bundles:
        _output.info(
            "No reproducibility bundles. Create one with: exa reproduce build <model> <ver>"
        )
        return
    _output.print_table(
        "Reproducibility Bundles",
        ["Model", "Version", "Bundle", "Hash", "Signed", "Created"],
        [
            [
                b["model"],
                b["version"],
                str(b["bundle_version"]),
                b["manifest_hash"][:12],
                "yes" if b["signature"] else "no",
                str(b["created_at"]),
            ]
            for b in bundles
        ],
    )
