from __future__ import annotations

import difflib
import importlib.metadata
import os
from enum import StrEnum

import click
import typer
from rich.console import Console
from typer.core import TyperGroup

from examlops.cli import _output, _plugins
from examlops.cli.commands import (
    ab_cmd,
    approvals,
    ask_cmd,
    autopilot_cmd,
    batch_cmd,
    cards_cmd,
    config_cmd,
    data_cmd,
    docs_cmd,
    doctor,
    drift,
    engines_cmd,
    env_cmd,
    eval_cmd,
    explain_cmd,
    explain_command,
    features_cmd,
    feedback_cmd,
    finops_cmd,
    gateway_cmd,
    genai_cmd,
    guardrails_cmd,
    hpc_cmd,
    hpo_cmd,
    mcp_cmd,
    models,
    modelzoo,
    namespace_cmd,
    pipeline,
    plugins_cmd,
    policy_cmd,
    predict,
    production,
    project_cmd,
    prompt_cmd,
    providers_cmd,
    quality_cmd,
    rag_cmd,
    retrain,
    rollback_cmd,
    scaffold,
    seanerbus_cmd,
    secrets_cmd,
    serve,
    shadow_cmd,
    stack,
    status,
    supplychain_cmd,
    vector_cmd,
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


class SuggestGroup(TyperGroup):
    """Typer group that adds fuzzy 'Did you mean …' suggestions on unknown commands.

    Modern Click (>=8.2) already suggests near-misses; this is a graceful fallback for
    older Click and never doubles up Click's own suggestion.
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            message = exc.message or ""
            if "did you mean" not in message.lower():
                typed = args[0] if args else ""
                matches = difflib.get_close_matches(typed, self.list_commands(ctx), n=3, cutoff=0.5)
                if matches:
                    hint = ", ".join(repr(m) for m in matches)
                    exc.message = f"{message} Did you mean {hint}?"  # type: ignore[misc]
            raise


app = typer.Typer(
    name="exa",
    cls=SuggestGroup,
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


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    yaml = "yaml"
    csv = "csv"


@app.callback()
def main(
    output: OutputFormat = typer.Option(
        OutputFormat.table,
        "--output",
        "-o",
        help="Output format: table (human) | json | yaml | csv (for scripting/agents)",
    ),
    json: bool = typer.Option(
        False, "--json", help="Shorthand for --output json (kept for compatibility)"
    ),
    context: str = typer.Option(
        "", "--context", "-c", help="Use a named config context for this invocation"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirmation prompts"),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress non-essential output (hints, info, progress detail)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show extra diagnostic detail"),
    version: bool = typer.Option(  # noqa: FBT001
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit",
    ),
) -> None:
    fmt = "json" if json else output.value
    _output.output_format = fmt
    # json_mode gates every existing structured-output branch; true for any non-table format.
    _output.json_mode = fmt != "table"
    _output.yes_mode = yes
    _output.quiet_mode = quiet
    _output.verbose_mode = verbose
    # A one-off --context is exposed to load_config() via the same env var it already reads.
    if context:
        os.environ["EXAMLOPS_CONTEXT"] = context
    try:
        _init_platform_db()
    except Exception:
        pass  # non-fatal: DB may not be writable in some envs


app.add_typer(approvals.app, name="approvals", help="Sysadmin approval gate")
app.add_typer(
    autopilot_cmd.app,
    name="autopilot",
    help="Self-driving MLOps closed loop (detect→retrain→promote, policy-governed)",
)
app.add_typer(
    data_cmd.app,
    name="data",
    help="Dataset versioning & reproducibility (revisions, diff, checkout)",
)
app.add_typer(drift.app, name="drift", help="Prediction drift detection")
app.add_typer(models.app, name="models", help="MLflow model registry")
app.add_typer(modelzoo.app, name="modelzoo", help="ModelZoo repository freshness and events")
app.add_typer(production.app, name="production", help="Production deployment and verification")
app.add_typer(serve.app, name="serve", help="Ray Serve operations")
app.add_typer(pipeline.app, name="pipeline", help="Prefect training pipeline")
pipeline.app.add_typer(quality_cmd.app, name="quality", help="Data quality validation gates")
app.add_typer(stack.app, name="stack", help="Docker Compose stack")
app.add_typer(config_cmd.app, name="config", help="CLI configuration")
app.add_typer(seanerbus_cmd.app, name="seanerbus", help="SeanerBUS bridge UUID management")
app.add_typer(namespace_cmd.app, name="namespace", help="Project namespace isolation")
app.add_typer(
    project_cmd.app, name="project", help="ExaMLOps Projects (CPU/memory/storage/GPU quotas)"
)
serve.app.add_typer(shadow_cmd.app, name="shadow", help="Shadow deployment traffic mirroring")
serve.app.add_typer(batch_cmd.app, name="batch", help="Batch inference jobs")
serve.app.add_typer(ab_cmd.app, name="ab", help="A/B testing experiments")
models.app.add_typer(cards_cmd.app, name="card", help="Generate model cards")
# D3 — merge sign/verify/bom directly into the `exa models` group (not a sub-group).
models.app.registered_commands.extend(supplychain_cmd.app.registered_commands)
# E2 — `exa models quantize` + `exa models engine …` (validate/list).
models.app.registered_commands.extend(engines_cmd.app.registered_commands)
models.app.registered_groups.extend(engines_cmd.app.registered_groups)
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
# C2/C3 — `exa eval run` (suites) + `exa eval gate …` (regression gate).
eval_app.registered_commands.extend(eval_cmd.app.registered_commands)
eval_app.registered_groups.extend(eval_cmd.app.registered_groups)
app.add_typer(eval_app, name="eval", help="Continuous evaluation and feedback")

app.add_typer(finops_cmd.app, name="finops", help="FinOps + Green-AI budgets and carbon accounting")
app.add_typer(
    gateway_cmd.app, name="gateway", help="Model gateway — virtual keys, routing, and cost"
)
app.add_typer(
    genai_cmd.app, name="genai", help="GenAI observability (OpenTelemetry semconv) + token cost"
)
app.add_typer(
    vector_cmd.app, name="vector", help="Vector store — collections, upsert, search, reindex"
)
app.add_typer(rag_cmd.app, name="rag", help="RAG — ingest knowledge bases and query with citations")
app.add_typer(
    guardrails_cmd.app, name="guardrails", help="Guardrails — injection/PII/toxicity defense"
)
app.add_typer(
    prompt_cmd.app, name="prompt", help="Prompt registry — versioned templates + labels (dev/prod)"
)
app.add_typer(
    secrets_cmd.app, name="secrets", help="Secrets management, rotation, and leak scanning"
)
app.add_typer(hpc_cmd.app, name="hpc", help="HPC fleet — discover schedulers, nodes, and GPUs")
app.add_typer(mcp_cmd.app, name="mcp", help="MCP server + Agent-to-Agent (A2A) surface")
app.add_typer(
    providers_cmd.app, name="providers", help="Pluggable calculation providers (all domains)"
)
app.add_typer(
    policy_cmd.app, name="policy", help="Policy-as-code — declarative governance for mutations"
)

app.command("ask", epilog=ask_cmd._EXAMPLES)(ask_cmd.ask)
app.command("explain", epilog=explain_command._EXAMPLES)(explain_command.explain)
app.command("env", epilog=env_cmd._EXAMPLES)(env_cmd.env)
app.command("retrain", epilog=retrain._EXAMPLES)(retrain.retrain)
app.command("predict", epilog=predict._EXAMPLES)(predict.predict)
app.command("scaffold", epilog=scaffold._EXAMPLES)(scaffold.scaffold)
app.command("status", epilog=status._EXAMPLES)(status.status)
app.command("audit", epilog=audit_cmd._EXAMPLES)(audit_cmd.audit)
app.command("doctor", epilog=doctor._EXAMPLES)(doctor.doctor)
app.command("plugins", epilog=plugins_cmd._EXAMPLES)(plugins_cmd.plugins)
app.command("docs", epilog=docs_cmd._EXAMPLES)(docs_cmd.docs)

# Third-party subcommands via entry points (examlops.cli_plugins). Resilient to failures.
try:
    _plugins.register(app)
except Exception:  # pragma: no cover - never let plugin discovery break the CLI
    pass
