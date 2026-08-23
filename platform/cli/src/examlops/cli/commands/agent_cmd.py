"""``exa agent`` — is the Skipper agent up, and which brain is it actually using?

Why this exists
---------------
``exa ask`` could talk to the agent but nothing could *interrogate* it. That gap had a cost:
the Azure key behind Skipper expired, and because the agent still answered every request —
with an empty string — the failure looked like a bad model rather than a rejected credential.
It sat that way until someone ran a 30-question evaluation and got 0/30. A one-line status
command would have named it immediately.

So this reports the four things that decide whether an answer can be trusted, and it reports
them from the **server**, not from this machine's environment: which backend was resolved,
which model, whether the long-term memory store actually attached, and — when the backend is
unusable — the specific environment variable to repair. Reading the local ``.env`` would be
worse than useless here, because the agent normally runs somewhere else (a container, the LXP
node) with entirely different values.

The agent's ``GET /api/info`` already computes all of it by probing the backend for real, so
this command is a thin client over the same HTTP surface ``exa ask`` uses. Nothing imports
``skipper``: the CLI ships in ``examlops``, the agent is a separate service, and coupling them
would make ``exa`` unusable wherever the agent is not installed — which is most places.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Is the agent up, and on which backend?[/dim]\n"
    "  exa agent status\n\n"
    "  [dim]# Machine-readable, for a health check or CI gate[/dim]\n"
    "  exa --json agent status\n\n"
    "  [dim]# Point at an agent running elsewhere[/dim]\n"
    "  AGENT_URL=http://lxp-cpu01:18004 exa agent status\n\n"
    "  [dim]# Then talk to it[/dim]\n"
    '  exa ask "which models are drifting?"\n\n'
    "  [dim]# Or hold a conversation[/dim]\n"
    "  exa chat"
)

_CHAT_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Interactive conversation with the agent[/dim]\n"
    "  exa chat\n\n"
    "  [dim]# Against the agent in another environment[/dim]\n"
    "  exa -c lxp chat\n\n"
    "  [dim]# Pass options straight through to kq[/dim]\n"
    "  exa chat -- --resume last"
)

app = typer.Typer(
    help="Skipper agent — health, backend and memory",
    no_args_is_help=True,
    epilog=_EXAMPLES,
    rich_markup_mode="rich",
)


@app.callback()
def _agent() -> None:
    """Skipper agent — health, backend and memory.

    An explicit callback keeps this a *group* even while it holds a single command. Typer
    otherwise collapses a one-command app into the command itself, which would make
    ``exa agent status`` resolve by accident rather than by design — and would silently
    change shape the moment a second command lands here.
    """


@app.command("status", epilog=_EXAMPLES)
def status() -> None:
    """Show the agent's reachability, LLM backend, model and memory tier.

    Exits non-zero when the agent is unreachable *or* when it is up but its backend is
    unusable. Both mean "do not trust an answer from this agent", which is the question a
    script is really asking, and collapsing them into one exit code is what makes this
    usable as a health gate.
    """
    cfg = load_config()
    base = cfg.agent_url.rstrip("/")
    token = os.getenv("AGENT_API_KEY", "")

    try:
        info = _client.get(f"{base}/api/info", token=token)
    except _client.ClientError as exc:
        if _output.json_mode:
            _output.print_json({"reachable": False, "url": cfg.agent_url, "error": str(exc)})
            raise typer.Exit(1) from None
        _output.error(
            f"Could not reach the Skipper agent at {cfg.agent_url}: {exc}",
            hint="Start it with: make skipper-server   "
            "(or set AGENT_URL / exa config set agent <url>)",
        )
        return

    if not isinstance(info, dict):  # a proxy or login page answering on the port
        _output.error(
            f"{base}/api/info did not return an object — is {cfg.agent_url} really the agent?"
        )
        return

    backend_ok = bool(info.get("ok"))
    memory = info.get("memory") or {}
    record = {
        "reachable": True,
        "url": cfg.agent_url,
        "backend": info.get("backend"),
        "model": info.get("model"),
        "backend_ok": backend_ok,
        "memory_enabled": bool(memory.get("enabled")),
        # `active` is the honest answer: the compiled graph either got a store or it did not.
        # Enabled-but-not-active is the interesting state — it points at the embedding backend.
        "memory_active": bool(memory.get("active")),
    }
    if info.get("skipped"):
        record["backends_skipped"] = info["skipped"]
    if info.get("fix"):
        record["fix"] = info["fix"]

    if _output.json_mode:
        _output.print_json(record)
        if not backend_ok:
            raise typer.Exit(1)
        return

    tick = "[green]✓[/green]"
    cross = "[red]✗[/red]"
    rows = [
        ["Endpoint", cfg.agent_url],
        ["Reachable", f"{tick} yes"],
        ["Backend", f"{info.get('backend', '?')}  {tick if backend_ok else cross}"],
        ["Model", str(info.get("model", "?"))],
        [
            "Memory",
            _memory_line(bool(memory.get("enabled")), bool(memory.get("active")), tick, cross),
        ],
    ]
    if record.get("backends_skipped"):
        rows.append(["Rejected first", ", ".join(record["backends_skipped"])])
    _output.print_table("Skipper agent", ["", ""], rows)

    if not backend_ok:
        _output.error(
            f"The agent is running but its {info.get('backend')} backend is not usable — "
            "it will answer, and the answers will be worthless.",
            hint=str(info.get("fix") or "Check the agent's LLM credentials."),
        )
        return
    if memory.get("enabled") and not memory.get("active"):
        _output.warning(
            "Long-term memory is enabled but did not attach — the agent is running on "
            "conversation memory only."
        )
        _output.hint(
            "Usually the embedding backend: check AGENT_EMBED_BACKEND / AGENT_EMBED_MODEL, "
            "then re-run 'make skipper-knowledge-ingest'."
        )


def _memory_line(enabled: bool, active: bool, tick: str, cross: str) -> str:
    """Render the three states apart, because the middle one is the diagnostic.

    ``off`` is a choice, ``active`` is healthy, and *enabled but not attached* is the failure
    that otherwise shows up only as answers that are quietly worse than they should be.
    """
    if not enabled:
        return "off (short-term only)"
    if active:
        return f"{tick} long-term store attached"
    return f"{cross} enabled but NOT attached — short-term only"


def chat(
    ctx: typer.Context,
    kq_args: list[str] = typer.Argument(
        None,
        help="Extra arguments passed straight through to kq (put them after --)",
        metavar="[-- KQ_ARGS...]",
    ),
) -> None:
    """Hold an interactive conversation with the Skipper agent.

    A launcher, deliberately — not a chat client. ExaMLOps already decided this question and
    wrote the answer down in ``platform/services/agent/kube-q/README.md``: the terminal client
    is `kube-q <https://github.com/MSKazemi/kube_q>`_ (``kq``), used **unforked from PyPI**, and
    the platform adapts *to it* by exposing an OpenAI-compatible bridge on the agent server. One
    binary drives ExaMLOps, KubeIntellect, or any other agentic backend by URL.

    Writing a second REPL here would contradict that and lose everything ``kq`` already has —
    session history and resume, full-text search across past conversations, conversation
    branching, ``/approve`` and ``/deny`` for the human-in-the-loop gate, token and cost
    accounting, Rich rendering. All of that arrives for the cost of resolving one URL.

    What this adds over ``make skipper-chat`` is the thing the Makefile cannot do: it honours
    the CLI's own configuration, so ``exa -c lxp chat`` talks to the agent in the *lxp* context
    without anyone editing a profile or exporting a variable.
    """
    if _output.json_mode:
        _output.error(
            "exa chat is interactive and has no machine-readable form.",
            hint='Use: exa --json ask "<question>"   (or: exa --json agent status)',
        )
        return

    kq = shutil.which("kq") or shutil.which("kq", path=os.path.dirname(sys.executable))
    if not kq:
        _output.error(
            "The kq terminal client is not installed.",
            hint="Install it with: uv pip install kube-q   "
            "(exa chat deliberately does not install it for you)",
        )
        return

    cfg = load_config()
    argv = [kq, "--url", cfg.agent_url.rstrip("/")]
    token = os.getenv("AGENT_API_KEY", "")
    if token:
        argv += ["--api-key", token]
    argv += list(kq_args or []) + list(ctx.args)

    _output.detail(f"Connecting to the Skipper agent at {cfg.agent_url} …")
    try:
        raise typer.Exit(subprocess.call(argv))
    except FileNotFoundError:  # pragma: no cover - shutil.which just found it
        _output.error(f"Could not execute {kq}.")
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
