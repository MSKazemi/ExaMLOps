from __future__ import annotations

import importlib.metadata

import typer
from rich.console import Console

from examlops.cli import _output
from examlops.cli.commands import (
    ab_cmd,
    approvals,
    batch_cmd,
    cards_cmd,
    config_cmd,
    doctor,
    drift,
    explain_cmd,
    features_cmd,
    feedback_cmd,
    finops_cmd,
    hpo_cmd,
    models,
    modelzoo,
    namespace_cmd,
    pipeline,
    predict,
    production,
    quality_cmd,
    retrain,
    rollback_cmd,
    scaffold,
    dataplane_cmd,
    serve,
    shadow_cmd,
    stack,
    status,
)
from examlops.cli.commands import (
    audit as audit_cmd,
)
from examlops.platform_db import init_db as _init_platform_db

_console = Console()

try:
    _VERSION = importlib.metadata.version("examlops")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"

_QUICK_START = (
    "[bold cyan]Quick start[/bold cyan]\n\n"
    "  [dim]# Check platform health[/dim]\n"
    "  exa status\n\n"
    "  [dim]# Trigger a training run[/dim]\n"
    "  exa retrain JPCP --dummy\n\n"
    "  [dim]# Inspect drift and production models[/dim]\n"
    "  exa drift status\n"
    "  exa models list\n\n"
    "  [dim]# Diagnose your setup[/dim]\n"
    "  exa doctor\n\n"
    "  [dim]# Get JSON output for scripting[/dim]\n"
    "  exa --json models list\n\n"
    "  [dim]# Skip confirmation prompts in CI[/dim]\n"
    "  exa --yes approvals reject JPCP --reason 'automated'"
)

app = typer.Typer(
    name="exa",
    help="ExaMLOps platform CLI — manage models, training, inference, and services.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog=_QUICK_START,
)


def _version_callback(value: bool) -> None:
    if value:
        _console.print(f"[bold]exa[/bold] version [cyan]{_VERSION}[/cyan]")
        raise typer.Exit()


@app.callback()
def main(
    json: bool = typer.Option(False, "--json", help="Output raw JSON (for scripting)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirmation prompts"),
    version: bool = typer.Option(  # noqa: FBT001
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit",
    ),
) -> None:
    _output.json_mode = json
    _output.yes_mode = yes
    try:
        _init_platform_db()
    except Exception:
        pass  # non-fatal: DB may not be writable in some envs


app.add_typer(approvals.app, name="approvals", help="Sysadmin approval gate")
app.add_typer(drift.app, name="drift", help="Prediction drift detection")
app.add_typer(models.app, name="models", help="MLflow model registry")
app.add_typer(modelzoo.app, name="modelzoo", help="ModelZoo repository freshness and events")
app.add_typer(production.app, name="production", help="Production deployment and verification")
app.add_typer(serve.app, name="serve", help="Ray Serve operations")
app.add_typer(pipeline.app, name="pipeline", help="Prefect training pipeline")
pipeline.app.add_typer(quality_cmd.app, name="quality", help="Data quality validation gates")
app.add_typer(stack.app, name="stack", help="Docker Compose stack")
app.add_typer(config_cmd.app, name="config", help="CLI configuration")
app.add_typer(dataplane_cmd.app, name="dataplane", help="DataPlane bridge UUID management")
app.add_typer(namespace_cmd.app, name="namespace", help="Project namespace isolation")
serve.app.add_typer(shadow_cmd.app, name="shadow", help="Shadow deployment traffic mirroring")
serve.app.add_typer(batch_cmd.app, name="batch", help="Batch inference jobs")
serve.app.add_typer(ab_cmd.app, name="ab", help="A/B testing experiments")
models.app.add_typer(cards_cmd.app, name="card", help="Generate model cards")
models.app.add_typer(
    rollback_cmd.app, name="rollback", help="Roll back a model alias to a previous version"
)
serve.app.add_typer(explain_cmd.app, name="explain", help="Feature importance explanations (XAI)")
pipeline.app.add_typer(hpo_cmd.app, name="hpo", help="Hyperparameter optimisation")
app.add_typer(features_cmd.app, name="features", help="Feature store — versioned training features")

# Continuous evaluation & the ground-truth feedback loop (#9/#14).
eval_app = typer.Typer(
    help="Model evaluation — ground-truth feedback loop and live accuracy.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
eval_app.add_typer(feedback_cmd.app, name="feedback", help="Ground-truth feedback loop")
app.add_typer(eval_app, name="eval", help="Continuous evaluation and feedback")

app.add_typer(finops_cmd.app, name="finops", help="FinOps + Green-AI budgets and carbon accounting")

app.command("retrain", epilog=retrain._EXAMPLES)(retrain.retrain)
app.command("predict", epilog=predict._EXAMPLES)(predict.predict)
app.command("scaffold", epilog=scaffold._EXAMPLES)(scaffold.scaffold)
app.command("status", epilog=status._EXAMPLES)(status.status)
app.command("audit", epilog=audit_cmd._EXAMPLES)(audit_cmd.audit)
app.command("doctor", epilog=doctor._EXAMPLES)(doctor.doctor)
