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
    admission_cmd,
    agentops_cmd,
    approvals,
    ask_cmd,
    assets_cmd,
    autopilot_cmd,
    autoscale_cmd,
    backup_cmd,
    batch_cmd,
    cards_a6_cmd,
    cards_cmd,
    challenger_cmd,
    compliance_cmd,
    config_cmd,
    connection_cmd,
    data_cmd,
    distributed_cmd,
    docs_cmd,
    doctor,
    drift,
    embedding_cmd,
    engines_cmd,
    env_cmd,
    eval_cmd,
    events_cmd,
    exchange_cmd,
    explain_cmd,
    explain_command,
    fairness_cmd,
    feature_cmd,
    features_cmd,
    federated_cmd,
    feedback_cmd,
    finetune_cmd,
    finops_cmd,
    fleet_cmd,
    gateway_cmd,
    genai_cmd,
    governance_cmd,
    gpu_share_cmd,
    guardrails_cmd,
    hardware_cmd,
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
    report_cmd,
    reproduce_cmd,
    retrain,
    rollback_cmd,
    routing_cmd,
    scaffold,
    dataplane_cmd,
    secrets_cmd,
    serve,
    shadow_cmd,
    slo_cmd,
    stack,
    status,
    supplychain_cmd,
    vector_cmd,
    workbench_cmd,
)
from examlops.cli.commands import (
    audit as audit_cmd,
)
from examlops.data import init_db as _init_platform_db

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
pipeline.app.add_typer(
    distributed_cmd.app, name="distributed", help="Distributed training + checkpoint/resume (E6)"
)
app.add_typer(stack.app, name="stack", help="Docker Compose stack")
app.add_typer(config_cmd.app, name="config", help="CLI configuration")
app.add_typer(backup_cmd.app, name="backup", help="Backup / restore the platform datastore")
app.add_typer(
    events_cmd.app, name="events", help="NovaFabric event backbone (transactional outbox)"
)
app.add_typer(
    admission_cmd.app, name="admission", help="Admission-control queue (per-tenant fair-share)"
)
app.add_typer(report_cmd.app, name="report", help="Offline cost/carbon/SLA reports")
app.add_typer(fleet_cmd.app, name="fleet", help="Fleet Digital Twin — what-if simulation")
app.add_typer(
    exchange_cmd.app, name="exchange", help="NovaFabric Exchange — signed shareable packages"
)
app.add_typer(dataplane_cmd.app, name="dataplane", help="DataPlane bridge UUID management")
app.add_typer(namespace_cmd.app, name="namespace", help="Project namespace isolation")
app.add_typer(
    project_cmd.app, name="project", help="ExaMLOps Projects (CPU/memory/storage/GPU quotas)"
)
app.add_typer(
    connection_cmd.app, name="connection", help="Named Connections — reusable data sources (P2)"
)
app.add_typer(
    workbench_cmd.app,
    name="workbench",
    help="Project Workbenches — on-demand dev environments (P5)",
)
serve.app.add_typer(shadow_cmd.app, name="shadow", help="Shadow deployment traffic mirroring")
serve.app.add_typer(
    challenger_cmd.app, name="challenger", help="Champion-challenger scoreboard & promotion"
)
serve.app.add_typer(autoscale_cmd.app, name="autoscale", help="Autoscaling & scale-to-zero (E5)")
serve.app.add_typer(
    routing_cmd.app, name="routing", help="KV/prefix-cache-aware inference routing (E4)"
)
serve.app.add_typer(batch_cmd.app, name="batch", help="Batch inference jobs")
serve.app.add_typer(ab_cmd.app, name="ab", help="A/B testing experiments")
serve.app.add_typer(
    finetune_cmd.adapter_app,
    name="adapter",
    help="Multi-LoRA adapters (add/list/promote/route) (B7)",
)
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
app.add_typer(
    feature_cmd.app, name="feature", help="Feature store — one train/serve definition, no skew (A3)"
)
app.add_typer(
    assets_cmd.app, name="assets", help="Asset-centric pipelines — freshness DAG + rebuild (A4)"
)
app.add_typer(
    reproduce_cmd.app,
    name="reproduce",
    help="Reproducibility bundles — signed manifest + verify (A8)",
)
app.add_typer(
    embedding_cmd.app,
    name="embedding",
    help="Embedding lifecycle — encoders + blue-green reindex (B6)",
)
app.command("finetune", epilog=finetune_cmd._EXAMPLES)(finetune_cmd.finetune)
app.add_typer(
    federated_cmd.app,
    name="federated",
    help="Federated & privacy-preserving training — FedAvg/DP/secure-agg (E7)",
)
app.add_typer(
    hardware_cmd.app,
    name="hardware",
    help="Heterogeneous hardware & hybrid HPC↔cloud placement (E8)",
)
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
    agentops_cmd.app, name="agentops", help="AgentOps — agent trace & tool-call analytics"
)
app.add_typer(slo_cmd.app, name="slo", help="Model-quality SLOs — error budgets & burn-rate alerts")
app.add_typer(
    fairness_cmd.app, name="fairness", help="Fairness — subgroup performance & disparity monitoring"
)
app.add_typer(
    compliance_cmd.app, name="compliance", help="EU AI Act compliance — classify, Annex-IV, Art.12"
)
app.add_typer(
    governance_cmd.app, name="governance", help="NIST AI RMF control coverage & crosswalk"
)
app.add_typer(
    cards_a6_cmd.app, name="cards", help="Croissant dataset cards + structured model cards"
)
app.add_typer(
    prompt_cmd.app, name="prompt", help="Prompt registry — versioned templates + labels (dev/prod)"
)
app.add_typer(
    secrets_cmd.app, name="secrets", help="Secrets management, rotation, and leak scanning"
)
app.add_typer(hpc_cmd.app, name="hpc", help="HPC fleet — discover schedulers, nodes, and GPUs")
hpc_cmd.app.add_typer(
    gpu_share_cmd.app, name="gpu-share", help="Fractional GPU allocation & bin-packing (E3)"
)
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
app.add_typer(audit_cmd.app, name="audit", help="Audit log — tamper-evident, hash-chained (D4)")
app.command("doctor", epilog=doctor._EXAMPLES)(doctor.doctor)
app.command("plugins", epilog=plugins_cmd._EXAMPLES)(plugins_cmd.plugins)
app.command("docs", epilog=docs_cmd._EXAMPLES)(docs_cmd.docs)

# Third-party subcommands via entry points (examlops.cli_plugins). Resilient to failures.
try:
    _plugins.register(app)
except Exception:  # pragma: no cover - never let plugin discovery break the CLI
    pass
