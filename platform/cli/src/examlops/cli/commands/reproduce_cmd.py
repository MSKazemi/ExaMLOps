"""A8 — `exa reproduce`: signed reproducibility bundles (ADR 0038).

Capture a signed manifest of every input to a model version, rebuild + metric-verify within
a documented tolerance, and detect when a bundle is no longer reproducible. Bundles feed D1
technical docs and D2 evidence. No bit-exactness is claimed (GPU non-determinism).
"""

from __future__ import annotations

import json
import os
from typing import Any

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
    "  exa reproduce run JPCP 17 --execute --dummy --rtol 0.05\n\n"
    "  exa reproduce run JPCP 17 --execute --rebuild-env --dummy\n\n"
    "  exa reproduce run JPCP 17 --execute --restore-dataset ./restored --scheduler recorded\n\n"
    "  exa reproduce verify JPCP 17\n\n"
    "  exa reproduce show JPCP 17\n\n"
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
    image_digest: str = typer.Option(
        None, "--image-digest", help="Container image digest (default: EXAMLOPS_IMAGE_DIGEST)"
    ),
    feature_view: list[str] = typer.Option(
        None,
        "--feature-view",
        help="A3 feature view the model trains on (repeatable); pinned by definition hash",
    ),
    resources: str = typer.Option(
        None,
        "--resources",
        help='JSON scheduler request, e.g. \'{"nodes": 1, "gpus": 2}\' (EXAMLOPS_HPC_* keys)',
    ),
    scheduler: str = typer.Option(None, "--scheduler", help="Scheduler that ran the training"),
    lineage_run_id: str = typer.Option(None, "--lineage-run-id", help="A2 lineage run id"),
) -> None:
    """Capture + sign a reproducibility bundle for a model version (R1/R2).

    Commits (platform + model library), hardware, the image digest and the AI-BOM are
    collected automatically; the options add what only the operator knows.
    """
    from examlops.reproducibility import build_bundle
    from examlops.reproducibility.capture import UnknownFeatureViewError, capture_resources

    hp = json.loads(hyperparams) if hyperparams else None
    mt = json.loads(metrics) if metrics else None
    res = None
    if resources or scheduler:
        raw = json.loads(resources) if resources else {}
        if not isinstance(raw, dict):
            _output.error("--resources must be a JSON object.", exit_code=2)
        res = capture_resources(raw, scheduler=scheduler)
    try:
        bundle = build_bundle(
            model,
            version,
            dataset_name=dataset,
            dataset_revision=revision,
            hyperparams=hp,
            metrics=mt,
            seeds={"global": seed} if seed is not None else None,
            image_digest=image_digest,
            feature_views=list(feature_view) if feature_view else None,
            resources=res,
            lineage_run_id=lineage_run_id,
            actor=_actor(),
        )
    except UnknownFeatureViewError as exc:
        _output.error(f"Cannot build bundle: {exc.args[0] if exc.args else exc}", exit_code=1)
        return
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
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Really rebuild: checkout code, verify dataset + env, re-train, compare metrics",
    ),
    repo: str = typer.Option(
        ".", "--repo", help="[--execute] Git repo holding the bundle's commit"
    ),
    data_path: str = typer.Option(
        None, "--data-path", help="[--execute] Local data to verify against the pinned revision"
    ),
    dummy: bool = typer.Option(False, "--dummy", help="[--execute] Train on dummy data"),
    rebuild_env: bool = typer.Option(
        False,
        "--rebuild-env",
        help="[--execute] Really rebuild the recorded environment: install the bundle's package "
        "set into a fresh isolated venv (uv) and train on it, instead of comparing it with "
        "the caller's interpreter",
    ),
    allow_env_drift: bool = typer.Option(
        False,
        "--allow-env-drift",
        help="[--execute] Continue when the lockfile hash drifted, or the recorded container "
        "image is absent or mismatched",
    ),
    allow_dirty_code: bool = typer.Option(
        False,
        "--allow-dirty-code",
        help="[--execute] Rebuild even though the bundle was built from a dirty git tree",
    ),
    rtol: float = typer.Option(
        None, "--rtol", help="[--execute] Relative metric tolerance (default: bundle's, else 0.05)"
    ),
    train_cmd: str = typer.Option(
        None,
        "--train-cmd",
        help="[--execute] Custom training command run in the checkout; must print "
        "'EXAMLOPS_REPRO_METRICS=<json>' (default: the pipeline training flow)",
    ),
    timeout: int = typer.Option(3600, "--timeout", help="[--execute] Training timeout, seconds"),
    keep_worktree: bool = typer.Option(
        False, "--keep-worktree", help="[--execute] Keep the detached worktree for inspection"
    ),
    restore_dataset: str = typer.Option(
        None,
        "--restore-dataset",
        help="[--execute] Restore the pinned dataset (dataplane snapshot or lakeFS commit) into "
        "this empty directory; training sees it as EXAMLOPS_REPRO_DATA_DIR",
    ),
    scheduler: str = typer.Option(
        None,
        "--scheduler",
        help="[--execute] Where training runs: 'recorded' (the bundle's scheduler), 'mock', "
        "'slurm' or 'flux'. The recorded resources are re-requested either way "
        "(default: the caller's EXAMLOPS_HPC_SCHEDULER, else mock)",
    ),
) -> None:
    """Rebuild plan + metric-match within tolerance; --execute performs the rebuild (ADR 0038)."""
    if execute:
        _run_execute(
            model,
            version,
            repo=repo,
            data_path=data_path,
            dummy=dummy,
            rebuild_env=rebuild_env,
            allow_env_drift=allow_env_drift,
            allow_dirty_code=allow_dirty_code,
            rtol=rtol,
            train_cmd=train_cmd,
            timeout=timeout,
            keep_worktree=keep_worktree,
            restore_dataset=restore_dataset,
            scheduler=scheduler,
        )
        return
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


def _run_execute(model: str, version: str, **opts: Any) -> None:
    import shlex

    from examlops.reproducibility.execute import StepResult, execute_reproduction
    from examlops.reproducibility.image import STATUS_UNCHECKED as IMG_UNCHECKED
    from examlops.reproducibility.image import STATUS_UNVERIFIABLE as IMG_UNVERIFIABLE

    def show(step: StepResult) -> None:
        if not _output.json_mode:
            _output.info(f"[{step.step}] {step.status}: {step.detail}")

    cmd = opts.pop("train_cmd")
    res = execute_reproduction(
        model,
        version,
        train_cmd=shlex.split(cmd) if cmd else None,
        on_step=show,
        **opts,
    )
    if _output.json_mode:
        _output.print_json(
            {
                "model": res.model,
                "version": res.version,
                "ok": res.ok,
                "bit_exact": res.bit_exact,
                "rtol": res.rtol,
                "worktree": res.worktree,
                "produced_metrics": res.produced_metrics,
                "dataset_dir": res.dataset_dir,
                "modelzoo_worktree": res.modelzoo_worktree,
                "scheduler": res.scheduler,
                "resources": res.resources,
                "environment": {
                    "rebuilt": res.env.rebuilt,
                    "python": res.env.python,
                    "venv": res.env.venv,
                    "unsatisfied": res.env.unsatisfied,
                    "image_digest_status": res.env.image_status,
                    "image_digest_detail": res.env.image_detail,
                },
                "steps": [
                    {"step": s.step, "status": s.status, "detail": s.detail} for s in res.steps
                ],
            }
        )
        raise typer.Exit(0 if res.ok else 1)
    for s in res.steps:
        if s.status == "not_run":
            _output.info(f"[{s.step}] not_run: {s.detail}")
    _output.info(
        f"  · interpreter: {res.env.python}"
        + (" (rebuilt from the bundle's package set)" if res.env.rebuilt else " (caller's)")
    )
    _output.info(
        f"  · scheduler: {res.scheduler}"
        + (f" (re-requested {', '.join(sorted(res.resources))})" if res.resources else "")
    )
    if res.dataset_dir:
        _output.info(f"  · dataset restored to {res.dataset_dir}")
    if res.modelzoo_worktree:
        _output.info(f"  · model library checked out at {res.modelzoo_worktree}")
    if res.env.image_status == IMG_UNVERIFIABLE:
        _output.warning(f"  · container image digest UNVERIFIABLE: {res.env.image_detail}")
    elif res.env.image_status != IMG_UNCHECKED:
        _output.info(f"  · container image digest {res.env.image_status}: {res.env.image_detail}")
    if not res.ok:
        _output.error("Reproduction FAILED.", exit_code=1)
    _output.ok("Reproduced within tolerance (not bit-exact).")


@app.command("verify")
def verify(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
    allow_env_drift: bool = typer.Option(
        False,
        "--allow-env-drift",
        help="Report installed-package drift as a warning, not a failure",
    ),
) -> None:
    """Check referenced inputs still exist + hashes match (R5/GWT-4). Exit 1 if rotted."""
    from examlops.reproducibility import verify_bundle

    result = verify_bundle(model, version, allow_env_drift=allow_env_drift)
    if _output.json_mode:
        _output.print_json(
            {
                "model": result.model,
                "version": result.version,
                "reproducible": result.reproducible,
                "inputs": result.inputs,
                "problems": result.problems,
                "warnings": result.warnings,
            }
        )
        raise typer.Exit(0 if result.reproducible else 1)
    for w in result.warnings:
        _output.warning(w)
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


@app.command("show")
def show(
    model: str = typer.Argument(..., help="Model name"),
    version: str = typer.Argument(..., help="Model version"),
) -> None:
    """Show the latest bundle manifest for a model version (read-only)."""
    from examlops.data.data_assets import get_repro_bundle

    row = get_repro_bundle(model, version)
    if not row:
        _output.error(f"No bundle for {model}/{version}.", exit_code=1)
        return
    m = dict(row["manifest"])
    pkgs = (m.get("environment") or {}).get("packages") or {}
    if pkgs:  # the package set is bulky; the count is what a reader wants (--json keeps it all)
        summary = dict(m["environment"], packages=f"{len(pkgs)} recorded")
        shown = dict(m, environment=summary) if not _output.json_mode else m
    else:
        shown = m
    doc = {
        "model": row["model"],
        "version": row["version"],
        "bundle_version": row["bundle_version"],
        "manifest_hash": row["manifest_hash"],
        "signed": bool(row.get("signature")),
        "created_at": str(row.get("created_at")),
        "manifest": shown,
    }
    if _output.json_mode:
        _output.print_json(doc)
        return
    _output.info(json.dumps(doc, indent=2, default=str))


@app.command("list")
def list_cmd(
    model: str = typer.Argument(None, help="Filter by model"),
) -> None:
    """List reproducibility bundles."""
    from examlops.data.data_assets import list_repro_bundles

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
