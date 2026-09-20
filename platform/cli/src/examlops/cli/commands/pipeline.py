from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import EnvOverlay, StorageBackend
from examlops.cli._help import make_ordered_group
from examlops.cli.commands import hpo_cmd
from examlops.data import get_db, init_db
from examlops.data.audit import write_audit_event
from examlops.data.serving import set_promotion_rule
from examlops.promotion_gates import synthetic_only_gate_enabled, synthetic_only_training
from examlops.promotion_providers import resolve_promotion_eval_fn

# Help panels for `exa pipeline` (quality/distributed/hpo sub-groups attached in main.py).
_PANELS: list[tuple[str, list[str]]] = [
    ("Run & Deploy", ["run", "deploy", "list", "add-model", "export-registry"]),
    ("Validate & Promote", ["validate", "validate-model", "promote", "promote-delete"]),
    ("Advanced", ["hpo", "quality", "distributed"]),
]

app = typer.Typer(
    cls=make_ordered_group(_PANELS),
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_GENERATOR = "pipelines/pipeline_generator.py"
_DEPLOY = "pipelines/deploy.py"
_SCRIPT = "tools/scaffold_model.py"

_EXAMPLES_LIST = "Examples:\n\n  exa pipeline list"
_EXAMPLES_RUN = (
    "Examples:\n\n"
    "  # Fast dev run for every discovered model/dataset (no downloads)\n"
    "  exa pipeline run --dummy\n\n"
    "  # Copy-paste: train one model with dummy data\n"
    "  exa pipeline run --model JPCP --dataset PM100Dataset --dummy\n\n"
    "  # Train one model from MinIO-backed data\n"
    "  exa pipeline run --model JPCP --dataset PM100Dataset --backend minio\n\n"
    "  # Use YAML registry overlays for production settings\n"
    "  exa pipeline run --registry pipelines/model_registry.yaml --env prod"
)
_EXAMPLES_DEPLOY = (
    "Examples:\n\n"
    "  # Register all model deployments with the default nightly schedule\n"
    "  exa pipeline deploy\n\n"
    "  # Register deployments for manual triggering only\n"
    "  exa pipeline deploy --no-schedule\n\n"
    "  # Deploy one model from the YAML registry with staging settings\n"
    "  exa pipeline deploy --model JPCP --registry pipelines/model_registry.yaml --env staging"
)
_EXAMPLES_EXPORT = (
    "Examples:\n\n"
    "  # Snapshot auto-discovered models into pipelines/model_registry.yaml\n"
    "  exa pipeline export-registry"
)
_EXAMPLES_VALIDATE = (
    "Examples:\n\n"
    "  # Validate the pack's models/*.yaml against Python model shims\n"
    "  exa pipeline validate"
)


# These tools write their own output; `_output.run_external` streams it in table mode and turns it
# into one document under `--json`, where it used to land on stdout as raw text.


def _run_generator(args: list[str]) -> None:
    _output.run_external(
        [sys.executable, _GENERATOR, *args],
        not_found="pipeline_generator.py not found — run exa pipeline commands from the repo root",
        failed="Pipeline generator exited with code {code}",
    )


def _run_deploy(args: list[str]) -> None:
    _output.run_external(
        [sys.executable, _DEPLOY, *args],
        not_found="pipelines/deploy.py not found — run exa pipeline deploy from the repo root",
        failed="deploy.py exited with code {code}",
    )


def _run_pytest(args: list[str]) -> None:
    # `-p no:cacheprovider`: this runs in the operator's working directory, and a `.pytest_cache`
    # left behind there is litter the platform put in someone else's repository.
    _output.run_external(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        not_found="pytest not found — run make install-dev or uv pip install -e '.[dev]'",
        failed="pytest exited with code {code}",
    )


def _resolve_cluster_env(cluster: str, gpus: int) -> bool:
    """Resolve --cluster (name or 'auto') into EXAMLOPS_HPC_* env for the run subprocess.

    Returns True on success (env applied), False if the cluster is unknown/not approved or
    placement found no fit — in which case an error is printed and the run is aborted.
    """
    from examlops.hpc_registry import (
        ClusterNotActiveError,
        active_clusters_with_inventory,
        resolve_env,
    )

    target = cluster
    if cluster == "auto":
        from examlops.hpc_placement import ResourceAsk, choose_cluster
        from examlops.hpc_placement_providers import resolve_placement_score_fn

        result = choose_cluster(
            ResourceAsk(gpus=gpus), active_clusters_with_inventory(), resolve_placement_score_fn()
        )
        if result.cluster is None:
            _output.error(f"Auto-placement found no cluster: {result.reason}")
            return False
        _output.info(f"Auto-placement: {result.reason}")
        target = result.cluster

    try:
        env = resolve_env(target)
    except ClusterNotActiveError as exc:
        _output.error(str(exc))
        return False
    os.environ.update(env)
    _output.detail(f"  targeting cluster '{target}' → {env.get('EXAMLOPS_HPC_SCHEDULER')}")
    return True


@app.command("list", epilog=_EXAMPLES_LIST)
def list_pipelines():
    """List all auto-discovered models and their supported datasets."""
    _run_generator(["--list"])


@app.command(epilog=_EXAMPLES_RUN)
def run(
    model: str | None = typer.Option(None, "--model", "-m", help="Run for a single model only"),
    dataset: str | None = typer.Option(
        None, "--dataset", "-d", help="Run for a single dataset class only"
    ),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe)"),
    backend: StorageBackend | None = typer.Option(
        None, "--backend", "-b", help="Dataset storage backend"
    ),
    dataset_revision: str | None = typer.Option(
        None,
        "--dataset-revision",
        help="Pin training to a recorded dataset revision (see `exa data list`)",
    ),
    env: EnvOverlay | None = typer.Option(None, "--env", help="YAML registry env overlay"),
    registry: str | None = typer.Option(None, "--registry", help="Path to model_registry.yaml"),
    cluster: str | None = typer.Option(
        None,
        "--cluster",
        "-C",
        help="Target an ACTIVE HPC cluster by name, or 'auto' to let placement choose",
    ),
    gpus: int = typer.Option(
        0, "--gpus", "-g", help="GPUs to request (for --cluster auto placement)"
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Scope the run to a Project (ADR 0088): tags the run and attributes its cost",
    ),
):
    """Run training pipeline(s) locally via Prefect."""
    if cluster and not _resolve_cluster_env(cluster, gpus):
        return  # resolution failed / not approved — message already printed
    # P3 (ADR 0088): scope the run to a Project so its MLflow run + recorded cost are attributed.
    if project:
        os.environ["EXAMLOPS_PROJECT"] = project
        if model:
            from examlops.data import init_db as _init_db
            from examlops.data.projects import assign_resource_to_project

            _init_db()
            assign_resource_to_project(project, "model", model, added_by=os.getenv("USER"))
    # A1 (spec R12): a pinned revision must be materialisable — verify it was
    # recorded before we launch, and exit non-zero otherwise.
    if dataset_revision:
        if not dataset:
            _output.error("--dataset-revision requires --dataset to identify the pinned dataset.")
        from examlops.data import init_db as _init_db
        from examlops.data.data_assets import get_dataset_revision

        _init_db()
        if get_dataset_revision(dataset, dataset_revision) is None:
            _output.error(
                f"Dataset revision '{dataset_revision}' not found for {dataset} — "
                "cannot materialise. Run `exa data snapshot` / `exa data list` first."
            )
        os.environ["EXAMLOPS_DATASET_REVISION"] = dataset_revision
    args: list[str] = []
    if dummy:
        args.append("--dummy")
    if model:
        args += ["--model", model]
    if dataset:
        args += ["--dataset", dataset]
    if backend:
        args += ["--backend", backend]
    if registry:
        args += ["--registry", registry]
    if env:
        args += ["--env", env]
    _run_generator(args)


@app.command(epilog=_EXAMPLES_DEPLOY)
def deploy(
    no_schedule: bool = typer.Option(False, "--no-schedule", help="Deploy without a cron schedule"),
    model: str | None = typer.Option(None, "--model", "-m", help="Deploy for a single model only"),
    registry: str | None = typer.Option(None, "--registry", help="Path to model_registry.yaml"),
    env: EnvOverlay | None = typer.Option(None, "--env", "-e", help="Registry env overlay"),
):
    """Register Prefect deployments for all models (or one model)."""
    args: list[str] = []
    if no_schedule:
        args.append("--no-schedule")
    if model:
        args += ["--model", model]
    if registry:
        args += ["--registry", registry]
    if env:
        args += ["--env", env]
    _run_deploy(args)


@app.command("export-registry", epilog=_EXAMPLES_EXPORT)
def export_registry():
    """Export auto-discovered model state to pipelines/model_registry.yaml."""
    _run_generator(["--export-registry"])


@app.command(epilog=_EXAMPLES_VALIDATE)
def validate():
    """Validate the pack's models/*.yaml against Python model shims."""
    _run_pytest(["tests/unit/test_registry_integrity.py", "-v", "-k", "yaml"])


_EXAMPLES_VALIDATE_MODEL = (
    "Examples:\n\n"
    "  # Validate JPCP is responding within 2s latency threshold\n"
    "  exa pipeline validate-model JPCP\n\n"
    "  # Validate with custom latency threshold (500ms)\n"
    "  exa pipeline validate-model JPCP --max-latency 0.5\n\n"
    "  # Validate using Staging alias instead of Production\n"
    "  exa pipeline validate-model JPCP --alias Staging"
)

_DUMMY_EMBEDDING = [0.1] * 384


@app.command("validate-model", epilog=_EXAMPLES_VALIDATE_MODEL)
def validate_model(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
    alias: str = typer.Option("Staging", "--alias", help="Alias to validate"),
    max_latency: float = typer.Option(
        2.0, "--max-latency", help="Max acceptable latency in seconds"
    ),
    n_requests: int = typer.Option(3, "--n", help="Number of smoke-test requests"),
):
    """Smoke-test a model alias on Ray Serve and run the C3 eval gate against it.

    Returns exit code 0 on PASS, 1 on FAIL. Safe to use as a gate before promotion.

    ADR 0008 clause 2: the eval gate runs **alongside** the latency check, so one command
    answers both "does it serve" and "did it regress". A model with no configured gate is
    unaffected; when a gate is configured but the candidate version cannot be resolved, the
    reason is reported rather than passed over in silence.
    """
    import time

    cfg = load_config()
    body = {
        "embedding": _DUMMY_EMBEDDING,
        "num_nodes": 4,
        "user_id": "validate-gate",
        "model_name": model,
        "alias": alias,
    }

    latencies: list[float] = []
    errors: list[str] = []

    for i in range(n_requests):
        t0 = time.perf_counter()
        try:
            _client.post(f"{cfg.ray_serve_url}/infer-pipeline/infer", body)
            latencies.append(time.perf_counter() - t0)
        except _client.ClientError as e:
            errors.append(str(e))

    if errors:
        _output.error(f"Validation FAIL: {len(errors)}/{n_requests} requests failed — {errors[0]}")
        raise typer.Exit(1)

    avg_latency = sum(latencies) / len(latencies)
    max_observed = max(latencies)
    passed = avg_latency <= max_latency

    gate_row = _eval_gate_for_alias(cfg, model, alias)
    row = {
        "model": model,
        "alias": alias,
        "n_requests": n_requests,
        "avg_latency_s": round(avg_latency, 3),
        "max_latency_s": round(max_observed, 3),
        "threshold_s": max_latency,
        "result": "PASS" if passed else "FAIL",
        "eval_gate": gate_row["result"],
        "eval_gate_detail": gate_row["detail"],
    }

    if _output.json_mode:
        _output.print_json(row)
    else:
        cols = [
            "Model",
            "Alias",
            "Requests",
            "Avg Latency",
            "Max Latency",
            "Threshold",
            "Eval Gate",
            "Result",
        ]
        _output.print_table(
            "Model Validation",
            cols,
            [
                [
                    model,
                    alias,
                    str(n_requests),
                    f"{avg_latency:.3f}s",
                    f"{max_observed:.3f}s",
                    f"{max_latency}s",
                    f"{gate_row['result']} — {gate_row['detail']}",
                    row["result"],
                ]
            ],
        )

    if not passed:
        _output.error(f"Latency {avg_latency:.3f}s exceeds threshold {max_latency}s")
        raise typer.Exit(1)

    if gate_row["result"] == "FAIL":
        _output.error(f"Eval gate FAILED for {model}@{alias}: {gate_row['detail']}")
        raise typer.Exit(1)


def _eval_gate_for_alias(cfg, model: str, alias: str) -> dict[str, str]:
    """Run the C3 gate for whatever version *alias* points at (ADR 0008 clause 2).

    Returns a row for the validation report: ``result`` ∈ PASS / FAIL / SKIP, plus the reason.
    **SKIP is reported, never silent.** A CI log that shows only a green latency check, when the
    eval gate could not run, reads as "validated" — which is the failure this gate exists to
    prevent. The three skip reasons are distinct on purpose: no gate configured is a choice, an
    unresolvable alias is an outage, and a warn-mode gate is advisory by its own config.
    """
    from examlops.cli.commands.rollback_cmd import _get_current_alias_version
    from examlops.evaluation.gate import run_eval_gate

    try:
        gate = _lookup_eval_gate(model)
        if gate is None:
            return {"result": "SKIP", "detail": "no gate configured"}
        version = _get_current_alias_version(cfg, model, alias)
        if version is None:
            return {"result": "SKIP", "detail": f"could not resolve {model}@{alias} in MLflow"}
        result = run_eval_gate(model, str(version))
        if result is None:
            return {"result": "SKIP", "detail": "no gate configured"}
        failing = [m.name for m in result.metrics if m.failed]
        if result.passed and not failing:
            return {"result": "PASS", "detail": f"v{version} clean"}
        detail = f"v{version}: {', '.join(failing)}" if failing else f"v{version}"
        if result.passed:
            # warn mode, or an aggregate that outvoted the failures — both are passes that a
            # reader must be able to tell apart from "nothing failed".
            return {
                "result": "PASS",
                "detail": f"{detail} (not blocking: {result.mode}/{result.aggregate})",
            }
        return {"result": "FAIL", "detail": detail}
    except Exception as exc:  # noqa: BLE001 - a broken gate must not masquerade as a pass
        return {"result": "SKIP", "detail": f"gate error: {exc}"}


def _lookup_eval_gate(model: str):
    from examlops.data.evaluation import get_eval_gate

    return get_eval_gate(model)


_EXAMPLES_PROMOTE = (
    "Examples:\n\n"
    "  # Promote JPCP from Staging to Production if RMSE < 5.0\n"
    "  exa pipeline promote jpcp --if-rmse-lt 5.0\n\n"
    "  # Dry-run: show what would happen without promoting\n"
    "  exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run\n\n"
    "  # List saved promotion rules\n"
    "  exa pipeline promote --list"
)

_OPS: dict[str, object] = {
    "lt": lambda v, t: v < t,
    "gt": lambda v, t: v > t,
    "lte": lambda v, t: v <= t,
    "gte": lambda v, t: v >= t,
}


def _parity_actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command(
    epilog=_EXAMPLES_PROMOTE,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def promote(
    ctx: typer.Context,
    model: str | None = typer.Argument(None, help="Model name (e.g. jpcp); omit with --list"),
    from_alias: str = typer.Option("Staging", "--from", help="Source alias"),
    to_alias: str = typer.Option("Production", "--to", help="Target alias"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show outcome without promoting"),
    save: bool = typer.Option(False, "--save", help="Save rule to DB for future reference"),
    list_rules: bool = typer.Option(False, "--list", help="List saved promotion rules"),
    force: bool = typer.Option(
        False, "--force", help="Override a failing C3 eval gate (audited, D4)"
    ),
):
    """Promote a model alias when a metric threshold passes (rule-based gate).

    Specify metric threshold with --if-<metric>-<op> <value>, e.g. --if-rmse-lt 5.0
    """
    init_db()

    if list_rules:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM promotion_rules ORDER BY model").fetchall()
        if not rows:
            _output.ok("No saved promotion rules")
            return
        cols = ["Model", "Metric", "Op", "Threshold", "From", "To", "Enabled"]
        table = [
            [
                r["model"],
                r["metric"],
                r["operator"],
                r["threshold"],
                r["from_alias"],
                r["to_alias"],
                "yes" if r["enabled"] else "no",
            ]
            for r in rows
        ]
        _output.print_table("Promotion Rules", cols, table)
        return

    if not model:
        _output.error("Provide a model name or --list")
        return

    # Parse --if-<metric>-<op> VALUE from the extra args captured by context
    metric, operator, threshold = None, None, None
    extra = ctx.args
    for i, arg in enumerate(extra):
        if arg.startswith("--if-") and i + 1 < len(extra):
            parts = arg[5:].rsplit("-", 1)  # e.g. "rmse-lt" -> ["rmse", "lt"]
            if len(parts) == 2 and parts[1] in _OPS:
                metric, operator = parts[0], parts[1]
                try:
                    threshold = float(extra[i + 1])
                except ValueError:
                    _output.error(f"Invalid threshold: {extra[i + 1]}")
                    return
            break

    if metric is None or operator is None or threshold is None:
        _output.error("Specify threshold: --if-<metric>-<op> <value>  e.g. --if-rmse-lt 5.0")
        return

    cfg = load_config()

    try:
        rm_data = _client.get(
            f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/get"
            f"?name={urllib.parse.quote(model)}"
        )
    except _client.ClientError as e:
        _output.error(
            f"Failed to fetch model {model} from MLflow: {e}", hint="Is MLflow running? exa status"
        )
        return

    aliases = {
        a["alias"]: a["version"] for a in rm_data.get("registered_model", {}).get("aliases", [])
    }
    version = aliases.get(from_alias)
    if not version:
        _output.error(f"No version found under alias '{from_alias}' for {model}")
        return

    try:
        ver_data = _client.get(
            f"{cfg.mlflow_url}/api/2.0/mlflow/model-versions/get"
            f"?name={urllib.parse.quote(model)}&version={version}"
        )
        run_id = ver_data["model_version"]["run_id"]
        run_data = _client.get(f"{cfg.mlflow_url}/api/2.0/mlflow/runs/get?run_id={run_id}")
    except _client.ClientError as e:
        _output.error(str(e))
        return

    metrics_list = run_data.get("run", {}).get("data", {}).get("metrics", [])
    metrics = (
        {m["key"]: m["value"] for m in metrics_list}
        if isinstance(metrics_list, list)
        else metrics_list
    )
    raw_metric_val = metrics.get(metric)
    if raw_metric_val is None:
        _output.error(
            f"Metric '{metric}' not found in run {run_id}. Available: {list(metrics.keys())}"
        )
        return

    # MLflow serialises special values as the JSON strings "NaN"/"Infinity"; coerce to
    # float so comparison and formatting don't raise on a non-numeric metric value.
    try:
        metric_val = float(raw_metric_val)
    except (TypeError, ValueError):
        _output.error(
            f"Metric '{metric}' has a non-numeric value {raw_metric_val!r} in run {run_id}; "
            "cannot evaluate the promotion threshold."
        )
        return
    if metric_val != metric_val or metric_val in (float("inf"), float("-inf")):  # NaN/Inf
        _output.error(
            f"Metric '{metric}' is {raw_metric_val} (NaN/Inf) in run {run_id}; "
            "refusing to promote on a degenerate metric."
        )
        return

    _eval = resolve_promotion_eval_fn()
    passes, _reason = _eval(metric_val, threshold, operator)
    op_sym = "<" if operator in ("lt", "lte") else ">"
    status_str = f"{metric}={metric_val:.4f}  {op_sym}{threshold}"

    if dry_run:
        verdict = "WOULD promote" if passes else "Would NOT promote"
        _output.ok(f"[DRY RUN] {model} v{version}: {status_str}  → {verdict} to {to_alias}")
        return

    if not passes:
        _output.ok(f"Not promoted: {model} v{version}: {status_str}  (threshold not met)")
        return

    # Policy-as-code gate (ADR 0079): a `manual_promote` rule can deny or require approval.
    # No policy file / no matching rule -> allow, no audit row: behaviour is unchanged. A deny is
    # not overridable by --force (that flag only bypasses the built-in metric gates).
    from examlops.cli._policy_gate import enforce as _policy_enforce

    policy_decision = _policy_enforce(
        "manual_promote",
        {
            "model": model,
            "version": str(version),
            "from_alias": from_alias,
            "to_alias": to_alias,
            "metric": metric,
            "metric_value": metric_val,
            f"{metric}_new": metric_val,
            "operator": operator,
            "threshold": threshold,
            "force": force,
            "actor": _parity_actor(),
        },
        what=f"promotion of {model} v{version} to {to_alias}",
    )

    # C3 — eval regression gate: refuse to move the alias when a block-mode gate fails,
    # unless --force (which is audited). No configured gate → this is a no-op.
    from examlops.evaluation.gate import run_eval_gate

    higher_better = operator in ("gt", "gte")
    gate_result = run_eval_gate(model, str(version), higher_is_better=higher_better)
    if gate_result is not None and not gate_result.passed:
        failing = [m.name for m in gate_result.metrics if m.failed]
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        if not force:
            write_audit_event(
                "cli",
                actor,
                "promotion_blocked_by_gate",
                model,
                {"version": version, "failing_metrics": failing},
            )
            _output.error(
                f"Eval gate FAILED for {model} v{version}: {', '.join(failing)}. "
                "Use --force to override (audited).",
            )
            return
        write_audit_event(
            "cli",
            actor,
            "eval_gate_override",
            model,
            {"version": version, "to": to_alias, "failing_metrics": failing, "forced": True},
        )
        _output.warning(f"Eval gate FAILED but --force set; overriding: {', '.join(failing)}")

    # ADR 0117 — portability gate. Conditional on the promotion changing the execution
    # target, not a tax on every promotion: an unchanged target returns None and this is a
    # no-op. `inert` is deliberately *not* a pass — a quantisation that never ran cannot have
    # demonstrated parity, and letting it through is the vacuous-pass trap the ADR names.
    from examlops.parity import run_quantization_parity_gate

    parity = run_quantization_parity_gate(model, str(version), actor=_parity_actor())
    if parity is not None and not parity.permits_autonomous_promotion:
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        if not force:
            write_audit_event(
                "cli",
                actor,
                "promotion_blocked_by_parity",
                model,
                {"version": version, **parity.as_dict()},
            )
            _output.error(
                f"Portability gate {parity.verdict.upper()} for {model} v{version}: "
                f"{parity.reason}. Use --force to override (audited).",
                hint="exa models parity " + model,
            )
            return
        write_audit_event(
            "cli",
            actor,
            "parity_gate_override",
            model,
            {"version": version, "to": to_alias, "forced": True, **parity.as_dict()},
        )
        _output.warning(
            f"Portability gate {parity.verdict.upper()} but --force set; overriding: "
            f"{parity.reason}"
        )

    # C6 — SLO error-budget gate: when EXAMLOPS_SLO_GATE_ENABLED and a gate-flagged SLO
    # has an exhausted budget, refuse to promote (unless --force, audited). No-op otherwise.
    from examlops.cli.commands.slo_cmd import gate_enabled as _slo_gate_enabled

    if _slo_gate_enabled():
        from examlops.data.governance import list_slo_specs
        from examlops.slo import budget_exhausted, unmeasured_gates

        exhausted = [
            s["name"]
            for s in list_slo_specs(model=model)
            if s["gate_promotion"] and budget_exhausted(model, s["name"], tenant=s["tenant"])
        ]
        # A gate-flagged SLO with no samples cannot be evaluated, and passing it silently is
        # indistinguishable from a gate that ran and found the budget intact. It is reported
        # rather than enforced: no model can produce SLI samples before it serves, and none
        # serves before it is promoted, so refusing here would deadlock every first promotion.
        if unevaluated := unmeasured_gates(model):
            _output.warning(
                f"SLO gate could not evaluate {', '.join(unevaluated)} for {model}: no samples "
                "recorded. The gate is enabled but is not gating on these."
            )
        if exhausted:
            actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
            if not force:
                write_audit_event(
                    "cli",
                    actor,
                    "promotion_blocked_by_slo",
                    model,
                    {"version": version, "exhausted_slos": exhausted},
                )
                _output.error(
                    f"SLO budget exhausted for {model}: {', '.join(exhausted)}. "
                    "Use --force to override (audited).",
                )
                return
            write_audit_event(
                "cli",
                actor,
                "slo_gate_override",
                model,
                {"version": version, "to": to_alias, "exhausted_slos": exhausted, "forced": True},
            )
            _output.warning(
                f"SLO budget exhausted but --force set; overriding: {', '.join(exhausted)}"
            )

    # D1 — EU AI Act classification gate (R2): an in-scope system with no risk tier
    # MUST NOT be promoted until it is classified (unconditional; --force-overridable, audited).
    from examlops.compliance import promotion_blocked_reason

    compliance_reason = promotion_blocked_reason(model)
    if compliance_reason:
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        if not force:
            write_audit_event(
                "cli",
                actor,
                "promotion_blocked_by_compliance",
                model,
                {"version": version, "reason": compliance_reason},
            )
            _output.error(
                f"Promotion blocked: {compliance_reason}. Use --force to override (audited).",
            )
            return
        write_audit_event(
            "cli",
            actor,
            "compliance_gate_override",
            model,
            {"version": version, "reason": compliance_reason, "forced": True},
        )
        _output.warning(f"Compliance gate ({compliance_reason}) overridden with --force.")

    # C8 — fairness gate: when EXAMLOPS_FAIRNESS_GATE_ENABLED and a gate-flagged model
    # exceeds its disparity threshold, refuse to promote (unless --force, audited).
    if os.getenv("EXAMLOPS_FAIRNESS_GATE_ENABLED", "").lower() in ("1", "true", "yes", "on"):
        from examlops.fairness import fairness_gate

        if fairness_gate(model):
            actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
            if not force:
                write_audit_event(
                    "cli",
                    actor,
                    "promotion_blocked_by_fairness",
                    model,
                    {"version": version},
                )
                _output.error(
                    f"Fairness disparity exceeds threshold for {model}. "
                    "Use --force to override (audited).",
                )
                return
            write_audit_event(
                "cli",
                actor,
                "fairness_gate_override",
                model,
                {"version": version, "to": to_alias, "forced": True},
            )
            _output.warning("Fairness disparity exceeded but --force set; overriding.")

    # A7 — synthetic-only gate (spec R5): when EXAMLOPS_SYNTHETIC_ONLY_GATE is enabled and
    # the model was trained only on synthetic data, refuse to promote (unless --force, audited).
    if synthetic_only_gate_enabled():
        only_synth, synth_revs = synthetic_only_training(model)
        if only_synth:
            actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
            if not force:
                write_audit_event(
                    "cli",
                    actor,
                    "promotion_blocked_by_synthetic_only",
                    model,
                    {"version": version, "revisions": synth_revs},
                )
                _output.error(
                    f"{model} was trained only on synthetic data — refusing to promote to "
                    f"{to_alias}. Train on (or include) real data, or use --force (audited).",
                )
                return
            write_audit_event(
                "cli",
                actor,
                "synthetic_only_gate_override",
                model,
                {"version": version, "to": to_alias, "revisions": synth_revs, "forced": True},
            )
            _output.warning("Model is synthetic-only but --force set; overriding.")

    approval_note = " [policy requires approval]" if policy_decision.requires_approval else ""
    if not _output.confirm(
        f"Promote [bold]{model}[/bold] v{version} → [bold]{to_alias}[/bold]? "
        f"({status_str}){approval_note}",
        default=not policy_decision.requires_approval,
    ):
        _output.info("Cancelled.")
        return

    try:
        _client.post(
            f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/alias",
            {"name": model, "alias": to_alias, "version": version},
        )
    except _client.ClientError as e:
        _output.error(f"Promotion failed: {e}")
        return

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event(
        "cli",
        actor,
        "alias_promoted",
        model,
        {"from": from_alias, "to": to_alias, "version": version, metric: metric_val},
    )
    from examlops import events

    events.alias_changed(model, to_alias, version, actor=actor, via="exa-pipeline-promote")

    _emit_promotion_lineage(model, str(version), from_alias, to_alias, metric, metric_val)

    if save:
        set_promotion_rule(model, metric, operator, threshold, from_alias, to_alias)

    _output.ok(f"Promoted {model} v{version} → {to_alias}  ({status_str})")


def _emit_promotion_lineage(
    model: str, version: str, from_alias: str, to_alias: str, metric: str, value: float
) -> None:
    """A2 lineage for an alias move (ADR 0004 clause 1).

    The ADR names training, promotion and retrain as the emit paths and none of them called
    ``emit_lineage``. A promotion is the event that decides what serves traffic, so a provenance
    graph missing it cannot answer "what is in production and where did it come from".

    Emitted **after** the alias actually moved, so the graph records what happened rather than
    what was attempted; the whole call is fail-open, since lineage is bookkeeping and must never
    turn a successful promotion into a failed command.
    """
    try:
        from examlops.lineage import deployment_node, emit_lineage, eval_facet, model_node

        emit_lineage(
            "COMPLETE",
            job=f"promote:{model}",
            run_id=f"promote-{model}-v{version}-{to_alias}",
            inputs=[model_node(model, version)],
            outputs=[deployment_node(f"{model}@{to_alias}")],
            facets={**eval_facet(float(value), metric), "fromAlias": from_alias},
            model=model,
            model_version=version,
        )
    except Exception:  # noqa: BLE001 - never fail a completed promotion on bookkeeping
        pass


# `promote` parses its threshold flag out of ``ctx.args`` because the metric name is whatever
# the model logged to MLflow — there is no fixed list to declare as Click options. That makes
# the flag invisible to every introspection surface (`exa --json docs`, MCP tool generation,
# `platform/ci/adr_reconcile.py`, the Skipper prompt guard), each of which then reports a
# documented, working flag as missing. Declaring it here in machine-readable form is what
# `exa docs` reads, so the generated reference and the code cannot disagree.
# Attached to the function object on purpose (the introspection surfaces read it off the
# command); mypy has no way to declare an attribute added to a Callable after the fact.
promote.dynamic_options = [  # type: ignore[attr-defined]
    {
        "opts": "--if-<metric>-<op> <value>",
        "help": (
            "Promote only if the metric passes the threshold. <metric> is any metric the "
            "model logged; <op> is one of " + ", ".join(sorted(_OPS)) + ". "
            "Example: --if-rmse-lt 5.0"
        ),
    }
]


_EXAMPLES_ADD_MODEL = (
    "Examples:\n\n"
    "  # Register an existing modelzoo model into the pipeline (creates YAML + config)\n"
    "  exa pipeline add-model JPCP\n\n"
    "  # Specify task and type when they can't be auto-detected\n"
    "  exa pipeline add-model DemoAD --task anomaly_detection --type classification\n\n"
    "  # Overwrite existing YAML/config if you want to regenerate them\n"
    "  exa pipeline add-model JPCP --force"
)

_TASK_CHOICES = ["performance_prediction", "power_consumption_prediction", "anomaly_detection"]
_TYPE_CHOICES = ["regression", "classification"]


@app.command("add-model", epilog=_EXAMPLES_ADD_MODEL)
def add_model(
    name: str = typer.Argument(
        ..., help="PascalCase model name matching an existing modelzoo class"
    ),
    task: str = typer.Option(
        "performance_prediction", "--task", "-t", help=f"Task type [{' | '.join(_TASK_CHOICES)}]"
    ),
    task_type: str = typer.Option(
        "regression", "--type", "-T", help=f"ML type [{' | '.join(_TYPE_CHOICES)}]"
    ),
    promotion_metric: str = typer.Option("accuracy", "--metric", help="Promotion metric"),
    promotion_threshold: float = typer.Option(
        0.7, "--threshold", help="Production promotion threshold"
    ),
    promotion_direction: str = typer.Option(
        "higher_is_better", "--direction", help="[higher_is_better | lower_is_better]"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing YAML/config files"),
):
    """Register an existing modelzoo model into the training pipeline.

    Unlike [bold]exa scaffold[/bold], this command does NOT create a new model class.
    It only generates the pipeline YAML and config shim for a model class that
    already lives in modelzoo/seanergys_modelzoo/models/tasks/.

    Use this when you have written a model class by hand or imported one from
    the modelzoo and want to wire it into ExaMLOps training and inference.
    """
    import glob as _glob

    # Verify the model class exists in modelzoo before generating pipeline files.
    import os as _os

    modelzoo_tasks = _os.path.join("modelzoo", "seanergys_modelzoo", "models", "tasks")
    found_files = _glob.glob(
        _os.path.join(modelzoo_tasks, "**", f"{name.lower()}_model.py"), recursive=True
    )
    if not found_files:
        _output.error(
            f"Model class not found: expected a file matching "
            f"modelzoo/seanergys_modelzoo/models/tasks/**/{name.lower()}_model.py\n"
            f"  → Use exa scaffold {name} to create a new model from scratch."
        )
        raise typer.Exit(1)

    _output.ok(f"Found model class: {found_files[0]}")

    cmd = [
        sys.executable,
        _SCRIPT,
        "--name",
        name,
        "--task",
        task,
        "--task-type",
        task_type,
        "--promotion-metric",
        promotion_metric,
        "--promotion-threshold",
        str(promotion_threshold),
        "--promotion-direction",
        promotion_direction,
        "--skip-model-class",
    ]
    if force:
        cmd.append("--force")

    try:
        subprocess.run(cmd, check=True)  # noqa: S603
    except FileNotFoundError:
        _output.error(
            "tools/scaffold_model.py not found — run exa pipeline add-model from the repo root"
        )
    except subprocess.CalledProcessError as e:
        _output.error(f"add-model exited with {e.returncode}")


_EXAMPLES_PROMOTE_DELETE = (
    "Examples:\n\n  exa pipeline promote-delete JPCP\n\n  exa pipeline promote-delete --all"
)


@app.command("promote-delete", epilog=_EXAMPLES_PROMOTE_DELETE)
def promote_delete(
    model: str | None = typer.Argument(None, help="Model name (omit with --all)"),
    all_rules: bool = typer.Option(False, "--all", help="Delete ALL promotion rules"),
) -> None:
    """Delete saved metric-gated promotion rules."""
    from examlops.data import get_db as _get_db
    from examlops.data import init_db as _init_db

    _init_db()
    if all_rules:
        with _get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM promotion_rules").fetchone()[0]
        if count == 0:
            _output.ok("No promotion rules to delete")
            return
        if not _output.confirm(f"Delete all {count} promotion rule(s)? This cannot be undone."):
            _output.info("Cancelled.")
            return
        with _get_db() as conn:
            conn.execute("DELETE FROM promotion_rules")
        _output.ok(f"Deleted {count} promotion rule(s)")
        return
    if not model:
        _output.error(
            "Provide a model name or --all",
            hint="Examples: exa pipeline promote-delete JPCP  or  exa pipeline promote-delete --all",
        )
        raise typer.Exit(1)
    with _get_db() as conn:
        row = conn.execute("SELECT 1 FROM promotion_rules WHERE model=?", (model,)).fetchone()
        if not row:
            _output.error(
                f"No promotion rule found for {model}",
                hint="See saved rules: exa pipeline promote --list",
            )
            raise typer.Exit(1)
        conn.execute("DELETE FROM promotion_rules WHERE model=?", (model,))
    _output.ok(f"Deleted promotion rule for {model}")


app.add_typer(hpo_cmd.app, name="hpo", help="Hyperparameter optimisation")
