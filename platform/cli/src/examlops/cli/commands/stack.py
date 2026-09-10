from __future__ import annotations

import typer

from examlops.cli import _output
from examlops.cli._enums import StackService

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_COMPOSE_FILE = "platform/infra/docker-compose/docker-compose.yml"
_BASE_CMD = ["docker", "compose", "-f", _COMPOSE_FILE]

_EXAMPLES_UP = "Examples:\n\n  exa stack up\n\n  exa stack up --service dashboard"
_EXAMPLES_DOWN = "Examples:\n\n  exa stack down\n\n  exa stack down --service dashboard"
_EXAMPLES_RESTART = "Examples:\n\n  exa stack restart\n\n  exa stack restart --service ray-serving"
_EXAMPLES_LOGS = (
    "Examples:\n\n"
    "  exa stack logs\n\n"
    "  exa stack logs --service mlflow\n\n"
    "  exa stack logs --service dashboard --tail 100 --no-follow"
)
_EXAMPLES_STATUS = "Examples:\n\n  exa stack status"


def _parse_ps(text: str) -> list[dict]:
    """`docker compose ps --format json` prints an array, or one object per line (by version)."""
    import json

    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _dc(args: list[str], *, ps: bool = False) -> None:
    structured = ps and _output.json_mode
    _output.run_external(
        _BASE_CMD + args + (["--format", "json"] if structured else []),
        not_found="docker not found — is Docker installed?",
        failed="docker compose exited with {code}",
        parse=_parse_ps if structured else None,
    )


@app.command(epilog=_EXAMPLES_UP)
def up(
    service: StackService | None = typer.Option(
        None, "--service", "-s", help="Start only one service"
    ),
):
    """Start the ExaMLOps stack (or a single service)."""
    args = ["up", "-d", "--build"]
    if service:
        args.append(service)
    _dc(args)


@app.command(epilog=_EXAMPLES_DOWN)
def down(
    service: StackService | None = typer.Option(
        None, "--service", "-s", help="Stop only one service"
    ),
):
    """Stop the stack (or a single service)."""
    if service:
        _dc(["stop", service])
        _dc(["rm", "-f", service])
    else:
        _dc(["down"])


@app.command(epilog=_EXAMPLES_RESTART)
def restart(
    service: StackService | None = typer.Option(
        None, "--service", "-s", help="Restart only one service"
    ),
):
    """Restart the stack or a single service."""
    args = ["restart"]
    if service:
        args.append(service)
    _dc(args)


@app.command(epilog=_EXAMPLES_LOGS)
def logs(
    service: StackService | None = typer.Option(
        None, "--service", "-s", help="Show logs for one service"
    ),
    tail: int = typer.Option(50, "--tail", "-n", help="Number of lines to tail"),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f/-F", help="Follow log output"),
):
    """Tail docker compose logs."""
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    if service:
        args.append(service)
    _dc(args)


@app.command(epilog=_EXAMPLES_STATUS)
def status():
    """Show running containers and their ports."""
    _dc(["ps"], ps=True)


# ── Monitoring stack (same compose file, --profile monitoring) ──────────────

_MONITORING_SERVICES = ["prometheus", "grafana", "loki", "promtail", "alertmanager", "tempo"]


def _dc_monitoring(args: list[str], *, ps: bool = False) -> None:
    _dc(["--profile", "monitoring", *args], ps=ps)


@app.command("monitoring-up")
def monitoring_up():
    """Start monitoring stack: Prometheus, Grafana, Loki, Promtail, Alertmanager, Tempo."""
    _dc_monitoring(["up", "-d"] + _MONITORING_SERVICES)


@app.command("monitoring-down")
def monitoring_down():
    """Stop monitoring stack."""
    _dc_monitoring(["stop"] + _MONITORING_SERVICES)


@app.command("monitoring-status")
def monitoring_status():
    """Show monitoring stack container status."""
    _dc_monitoring(["ps"], ps=True)
