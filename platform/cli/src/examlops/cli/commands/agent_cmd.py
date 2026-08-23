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

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def _greet(url: str, info: object) -> None:
    """Say who is answering, and on what — the header kq's own banner cannot know.

    A banner that names the product is decoration; one that names the *backend actually
    resolved* is the thing an operator needs before trusting an answer. The Azure key behind
    Skipper once expired and the agent kept replying with empty strings, which read as a bad
    model rather than a rejected credential. ``/api/info`` has already been fetched to prove
    the agent is up, so this costs nothing.
    """
    if _output.quiet_mode:
        return
    facts = info if isinstance(info, dict) else {}
    brain = " · ".join(str(v) for v in (facts.get("backend"), facts.get("model")) if v)
    memory = (facts.get("memory") or {}) if isinstance(facts.get("memory"), dict) else {}
    _output.console.print("[bold]Skipper[/bold] [dim]— the ExaMLOps agent[/dim]")
    _output.console.print(f"[dim]  agent   {url}[/dim]")
    if brain:
        warn = "" if facts.get("ok") else "  [yellow](backend unusable)[/yellow]"
        _output.console.print(f"[dim]  brain   {brain}[/dim]{warn}")
    if memory:
        state = (
            "active" if memory.get("active") else ("enabled" if memory.get("enabled") else "off")
        )
        _output.console.print(f"[dim]  memory  {state}[/dim]")


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
        # `uv pip install 'examlops[chat]'` is the right instruction only if *this* install
        # declares the extra. A deploy whose source was synced without re-running the install
        # keeps the .dist-info it was built with, and uv answers an extra that metadata has
        # never heard of with "Checked 1 package" — no warning, nothing installed, and the
        # operator runs it four times because the remedy looked like it worked. Measured on
        # lxp-cpu01 2026-08-23: source v0.48.0, recorded metadata v0.46.0, no `chat` extra.
        # So ask our own metadata before naming the extra.
        try:
            extras = importlib.metadata.distribution("examlops").metadata.get_all("Provides-Extra")
            declared = "chat" in (extras or [])
        except Exception:  # pragma: no cover - running from a source tree with no dist-info
            declared = False
        if declared:
            hint = (
                "Install it with: uv pip install 'examlops[chat]'   "
                "(or `uv pip install kube-q` — exa chat launches it, it does not bundle it)"
            )
        else:
            hint = (
                "Install it with: uv pip install kube-q   "
                "(this install's metadata does not declare the 'chat' extra, so "
                "`uv pip install 'examlops[chat]'` would silently do nothing here — "
                "re-run `uv pip install -e platform/cli` to refresh it)"
            )
        _output.error("The kq terminal client is not installed.", hint=hint)
        return

    cfg = load_config()
    base = cfg.agent_url.rstrip("/")
    token = os.getenv("AGENT_API_KEY", "")

    # Ask the agent whether it is there before handing the terminal over. kq answers a refused
    # connection by opening its REPL in offline mode and retrying three times per message, so an
    # agent that was simply never started costs the operator a banner, a question, four timeouts
    # and a guess. The launcher knows which agent it meant; it can say so in one line.
    try:
        info = _client.get(f"{base}/api/info", token=token)
    except _client.ClientError as exc:
        _output.error(
            f"Could not reach the Skipper agent at {cfg.agent_url}: {exc}",
            hint="Start it with: make skipper-server   (or set AGENT_URL, or "
            "exa config set agent <url>). To open the client offline anyway, run kq directly.",
        )
        return

    argv = [kq, "--url", base]
    if token:
        argv += ["--api-key", token]
    passthrough = list(kq_args or []) + list(ctx.args)

    # kq is adopted **unforked**, and out of the box it introduces itself as Kube-Q, "your AI
    # co-pilot for Kubernetes", over an ASCII banner. That is the right default for the client
    # and the wrong greeting for an ExaMLOps operator, who is talking to Skipper about models,
    # drift and HPC jobs. The identity is supplied by the launcher rather than by forking the
    # client — which is the same bargain the rest of this command makes. Anything the caller
    # passes after ``--`` wins, so `exa chat -- --agent-name X` still does what it says.
    if not any(a.startswith("--agent-name") for a in passthrough):
        argv += ["--agent-name", "Skipper"]
    if not any(a in {"--banner", "--no-banner"} for a in passthrough):
        argv += ["--no-banner"]
    argv += passthrough

    _greet(cfg.agent_url, info)
    try:
        raise typer.Exit(subprocess.call(argv))
    except FileNotFoundError:  # pragma: no cover - shutil.which just found it
        _output.error(f"Could not execute {kq}.")
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


# ---------------------------------------------------------------------------
# exa agent memory — the erasure surface ADR 0034 accepted
# ---------------------------------------------------------------------------
# ADR 0034 (Accepted) makes a governance promise: an operator can *enumerate, export and
# erase* what the agent remembers, and it names `exa agent memory` as the surface. The
# capability shipped — `skipper.memory_admin` does all three, with cascade and an audit
# event — but only as `python -m skipper.memory_admin` run from inside the agent package.
# A right-to-erasure control that requires knowing where the service's source tree lives
# is not a control an operator has; the accepted decision was never actually delivered.
#
# It was not an oversight. This module's own docstring gives the reason: nothing here may
# import `skipper`, because `exa` ships wherever the agent does not and a hard dependency
# on langgraph would break the CLI everywhere else. That constraint is right, and it does
# not require the command to be missing — only that the import happen *inside* the command
# body, the same way `exa mcp` treats fastmcp. Absent the agent, this fails with a sentence
# that says so; present, it is the surface the ADR described.

_MEMORY_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# What does the agent remember, and how much of it?[/dim]\n"
    "  exa agent memory stats\n\n"
    "  [dim]# Enumerate one kind of memory[/dim]\n"
    "  exa agent memory list pref\n\n"
    "  [dim]# Everything one operator's name is attached to[/dim]\n"
    "  exa agent memory list pref --scope alice\n\n"
    "  [dim]# Export the whole store (subject access request)[/dim]\n"
    "  exa agent memory export --out memories.json\n\n"
    "  [dim]# Erase it, with cascade to derived memories (audited)[/dim]\n"
    "  exa agent memory delete pref --scope alice"
)

memory_app = typer.Typer(
    help="Enumerate, export and erase the agent's long-term memory (ADR 0034)",
    no_args_is_help=True,
    epilog=_MEMORY_EXAMPLES,
    rich_markup_mode="rich",
)


def _memory_admin():
    """Import the agent's memory admin, or explain precisely why it is unavailable.

    Returns the module. Raises ``typer.Exit(1)`` after printing an actionable error, so
    every caller here can treat a return value as usable.
    """
    import sys as _sys

    agent_dir = os.getenv("EXAMLOPS_AGENT_DIR") or str(
        Path(__file__).resolve().parents[6] / "platform" / "services" / "agent"
    )
    if agent_dir not in _sys.path:
        _sys.path.insert(0, agent_dir)
    try:
        from skipper import memory_admin  # noqa: PLC0415 - deliberately lazy, see above

        return memory_admin
    except ImportError as exc:
        _output.error(
            f"The Skipper agent package is not importable from here ({exc}).",
            hint=(
                "This command reads the agent's own memory store, so it must run where the "
                "agent is installed. Point at it with EXAMLOPS_AGENT_DIR=<repo>/platform/"
                "services/agent, or install the agent's requirements."
            ),
        )
        raise typer.Exit(1) from None


def _store():
    """Open the memory store and report which file was opened.

    Naming the path is not decoration. The store is a local file (``AGENT_MEMORY_DB``) and
    the agent usually runs somewhere else, so an operator who erases on the wrong host gets
    a success message and keeps the data. Saying which file was touched makes that visible.
    """
    admin = _memory_admin()
    try:
        return admin, admin.open_store()
    except Exception as exc:  # sqlite/langgraph both surface here
        _output.error(
            f"Could not open the agent memory store: {exc}",
            hint="Check AGENT_MEMORY_DB (default ./skipper_memory.db) — it is a local file.",
        )
        raise typer.Exit(1) from None


@memory_app.command("stats", epilog=_MEMORY_EXAMPLES)
def memory_stats() -> None:
    """Summarise what the agent remembers, by memory kind."""
    admin, store = _store()
    from skipper import config as _cfg  # noqa: PLC0415
    from skipper import memory_types

    data = memory_types.stats(store)
    if _output.json_mode:
        _output.print_json({"db": _cfg.AGENT_MEMORY_DB, **data})
        return
    rows = [[str(k), str(v)] for k, v in sorted(data.items())]
    _output.print_table(f"Agent memory — {_cfg.AGENT_MEMORY_DB}", ["Kind", "Items"], rows)


@memory_app.command("list", epilog=_MEMORY_EXAMPLES)
def memory_list(
    kind: str = typer.Argument(..., help="Memory kind: proc | episode | pref | kb"),
    scope: str = typer.Option(None, "--scope", help="Task-class / model / operator scope"),
    limit: int = typer.Option(50, "--limit", help="Maximum items to show"),
) -> None:
    """Enumerate stored memories of one kind."""
    admin, store = _store()
    from skipper import memory_types  # noqa: PLC0415

    if kind not in memory_types.KINDS:
        _output.error(
            f"Unknown memory kind {kind!r}.", hint=f"Choose one of: {', '.join(memory_types.KINDS)}"
        )
        raise typer.Exit(1)
    items = memory_types.list_kind(store, kind, scope=scope, limit=limit)
    records = [{"key": it.key, "text": it.value.get("text", "")} for it in items]
    if _output.json_mode:
        _output.print_json(records)
        return
    if not records:
        _output.detail(f"No {kind} memories" + (f" for scope {scope}" if scope else "") + ".")
        return
    _output.print_table(
        f"{kind} memories" + (f" · scope {scope}" if scope else ""),
        ["Key", "Text"],
        [[r["key"], r["text"][:96]] for r in records],
    )


@memory_app.command("export", epilog=_MEMORY_EXAMPLES)
def memory_export(
    out: str = typer.Option(None, "--out", help="Write JSON here instead of stdout"),
) -> None:
    """Export every stored memory as JSON — the subject-access half of ADR 0034."""
    admin, store = _store()
    from skipper import memory_types  # noqa: PLC0415

    data = memory_types.export_all(store)
    # export_all is keyed by memory kind, so len(data) is the number of kinds, not of
    # memories — count what the operator actually asked to see leave the building.
    total = sum(len(v) for v in data.values()) if isinstance(data, dict) else len(data)
    if out:
        Path(out).write_text(json.dumps(data, indent=2, default=str))
        _output.ok(f"Exported {total} memory item(s) to {out}")
        return
    _output.print_json(data)


@memory_app.command("delete", epilog=_MEMORY_EXAMPLES)
def memory_delete(
    kind: str = typer.Argument(..., help="Memory kind to erase"),
    scope: str = typer.Option(
        None, "--scope", help="Limit erasure to one scope (e.g. an operator)"
    ),
    operator: str = typer.Option(
        None, "--operator", help="Who is performing the erasure (audited)"
    ),
) -> None:
    """Erase memories, cascading to derived ones. Audited to ``audit_events``.

    The immutable audit log is a separate store and is deliberately *not* erased — ADR 0034
    keeps the record that an erasure happened while removing what was remembered.
    """
    admin, store = _store()
    from skipper import config as _cfg  # noqa: PLC0415
    from skipper import memory_types

    if kind not in memory_types.KINDS:
        _output.error(
            f"Unknown memory kind {kind!r}.", hint=f"Choose one of: {', '.join(memory_types.KINDS)}"
        )
        raise typer.Exit(1)
    target = f"all {kind} memories" + (f" for scope {scope!r}" if scope else "")
    # `_output.confirm` returns True under --json as well as --yes, which is right for the
    # mutations it was written for: a script that asked to promote a model meant it. Erasure
    # is the one case where that inference is unsafe — a monitoring script that adds --json to
    # read the store would delete it instead. So here --json alone is not consent; --yes is.
    if _output.json_mode and not _output.yes_mode:
        _output.error(
            "Refusing to erase without explicit consent.",
            hint="Erasure is irreversible, so --json alone is not taken as a yes. Add --yes.",
        )
        raise typer.Exit(1)
    if not _output.confirm(f"Erase {target} from {_cfg.AGENT_MEMORY_DB}? This cannot be undone."):
        _output.detail("Nothing erased.")
        return
    n = memory_types.erase(store, kind, scope=scope, operator=operator or _cfg.AGENT_ACTOR)
    if _output.json_mode:
        _output.print_json({"erased": n, "kind": kind, "scope": scope, "db": _cfg.AGENT_MEMORY_DB})
        return
    _output.ok(f"Erased {n} {kind} memory item(s)" + (f" for {scope}" if scope else ""))


app.add_typer(memory_app, name="memory")
