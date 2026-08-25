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

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import typer

from examlops.cli import _client, _output
from examlops.cli._config import active_project, load_config, scoped_agent_session

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
    "  [dim]# Resume a known server-side conversation[/dim]\n"
    "  exa chat --session incident-42\n\n"
    "  [dim]# Wait for complete answers instead of streaming tokens[/dim]\n"
    "  exa chat --no-stream"
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
    token = cfg.agent_token

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
    """Say who is answering and which backend the server actually resolved.

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


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_CHAT_HELP = """Commands:
  /help          Show this help
  /status        Show the agent, backend, memory, and current session
  /sessions      List server-side conversation IDs
  /history       Show the current conversation from the server
  /new [id]      Start a new conversation
  /resume ID     Continue a server-side conversation
  /approve       Approve the pending write action
  /deny          Reject the pending write action
  /quit          Leave chat
"""


def _new_session_id() -> str:
    return f"exa-{uuid.uuid4().hex[:12]}"


def _valid_session_id(value: str) -> bool:
    return bool(_SESSION_RE.fullmatch(value))


def _read_input(prompt: str) -> str:
    """One replaceable input seam keeps the interactive loop hermetic in tests."""
    return input(prompt)


def _chat_info(base: str, token: str) -> dict:
    try:
        info = _client.get(f"{base}/api/info", token=token)
    except _client.ClientError as exc:
        if exc.status == 401:
            _output.error(
                f"Authentication failed for the Skipper agent at {base}.",
                hint="Set AGENT_API_KEY or save a context-specific token with: "
                "exa config set agent_token",
            )
        _output.error(
            f"Could not reach the Skipper agent at {base}: {exc}",
            hint="Start it with: make skipper-server   "
            "(or set AGENT_URL / exa config set agent <url>)",
        )
    if not isinstance(info, dict):
        _output.error(f"{base}/api/info did not return an object — is this really the agent?")
    return info


def _print_chat_status(base: str, session_id: str, info: dict) -> None:
    memory = info.get("memory") if isinstance(info.get("memory"), dict) else {}
    rows = [
        ["Session", session_id],
        ["Project", active_project() or "(default)"],
        ["Endpoint", base],
        ["Ready", "yes" if info.get("ok") else "no"],
        ["Backend", info.get("backend", "?")],
        ["Model", info.get("model", "?")],
        [
            "Memory",
            "active"
            if memory.get("active")
            else ("enabled, not attached" if memory.get("enabled") else "off"),
        ],
    ]
    _output.print_table("Skipper chat", ["", ""], rows)


def _chat_completion(base: str, token: str, session_id: str, text: str, stream: bool) -> bool:
    """Send one turn, render it, and return whether a write is awaiting approval."""
    body = {
        "model": "examlops-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
        # The bridge uses this as the thread id when X-Session-ID is absent. Keeping the
        # identifier in the JSON body lets the shared HTTP client remain generic.
        "user": scoped_agent_session(session_id),
        "metadata": {"project": active_project()},
    }
    url = f"{base}/v1/chat/completions"
    if not stream:
        data = _client.post(url, body, token=token, timeout=300.0)
        if not isinstance(data, dict):
            raise _client.ClientError("the agent returned an invalid completion")
        if "error" in data:
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise _client.ClientError(str(message))
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise _client.ClientError("the agent returned a completion with no answer")
        choice = choices[0]
        message = choice.get("message") or {}
        answer = message.get("content", "") if isinstance(message, dict) else ""
        if not str(answer).strip():
            raise _client.ClientError("the agent returned an empty answer")
        _output.console.print(str(answer), markup=False, highlight=False)
        return bool(choice.get("hitl_required"))

    parts: list[str] = []
    hitl = False
    remote_error = ""
    for frame in _client.post_sse(url, body, token=token, timeout=300.0):
        event = frame.get("ki_event")
        if isinstance(event, dict):
            kind = event.get("type")
            message = str(event.get("message", ""))
            if kind == "tool_call":
                _output.console.print(f"[dim]· {message}[/dim]")
            elif kind == "error":
                remote_error = message or "the agent reported an error"
            continue
        choices = frame.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        hitl = hitl or bool(choice.get("hitl_required"))
        piece = (choice.get("delta") or {}).get("content") or ""
        if piece:
            parts.append(str(piece))
            _output.console.print(str(piece), end="", markup=False, highlight=False)
    if parts:
        _output.console.print()
    if remote_error:
        raise _client.ClientError(remote_error)
    if not parts and not hitl:
        raise _client.ClientError("the agent returned an empty answer")
    return hitl


def _print_sessions(base: str, token: str) -> None:
    data = _client.get(f"{base}/api/threads", token=token)
    threads = data.get("threads", []) if isinstance(data, dict) else None
    if not isinstance(threads, list):
        raise _client.ClientError("the agent returned an invalid conversation list")
    project = active_project()
    if project:
        prefix = f"{project}:"
        threads = [str(item)[len(prefix) :] for item in threads if str(item).startswith(prefix)]
    if not threads:
        _output.info("No server-side conversations found.")
        return
    _output.print_table("Skipper conversations", ["Session"], [[item] for item in threads])


def _print_history(base: str, token: str, session_id: str) -> None:
    encoded = quote(scoped_agent_session(session_id), safe="")
    data = _client.get(f"{base}/api/threads/{encoded}/history", token=token)
    messages = data.get("messages", []) if isinstance(data, dict) else None
    if not isinstance(messages, list):
        raise _client.ClientError("the agent returned invalid conversation history")
    if not messages:
        _output.info(f"No history found for {session_id}.")
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = {"human": "You", "ai": "Skipper", "tool": "Tool"}.get(
            str(message.get("role", "")), "Message"
        )
        name = f" · {message['name']}" if message.get("name") else ""
        _output.console.print(f"[bold]{role}{name}[/bold]")
        _output.console.print(str(message.get("content", "")), markup=False, highlight=False)


def chat(
    session: str | None = typer.Option(
        None, "--session", "-s", help="Server-side conversation ID to create or resume"
    ),
    stream: bool = typer.Option(
        True,
        "--stream/--no-stream",
        help="Stream answer tokens as they arrive",
    ),
) -> None:
    """Hold an interactive conversation with the Skipper agent."""
    if _output.json_mode:
        _output.error(
            "exa chat is interactive and has no machine-readable form.",
            hint='Use: exa --json ask "<question>"   (or: exa --json agent status)',
        )

    cfg = load_config()
    base = cfg.agent_url.rstrip("/")
    token = cfg.agent_token
    session_id = session or _new_session_id()
    if not _valid_session_id(session_id):
        _output.error(
            f"Invalid session id: {session_id!r}.",
            hint="Use 1-128 letters, digits, dots, underscores, colons, or hyphens.",
        )
    info = _chat_info(base, token)
    if not info.get("ok"):
        _output.error(
            f"The Skipper agent is reachable but its {info.get('backend', 'LLM')} backend "
            "is not usable.",
            hint=str(info.get("fix") or "Check the agent's LLM credentials."),
        )
    _greet(cfg.agent_url, info)
    _output.console.print(f"[dim]  session {session_id}[/dim]")
    if project := active_project():
        _output.console.print(f"[dim]  project {project}[/dim]")
    _output.console.print("[dim]Type /help for commands; /quit to leave.[/dim]")

    while True:
        try:
            text = _read_input(f"You [{session_id}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            _output.console.print("\n[dim]Goodbye.[/dim]")
            return
        if not text:
            continue

        command, _, argument = text.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command in {"/quit", "/exit", "/q"}:
            _output.console.print("[dim]Goodbye.[/dim]")
            return
        if command == "/help":
            _output.console.print(_CHAT_HELP, markup=False, highlight=False)
            continue
        if command == "/status":
            try:
                info = _chat_info(base, token)
                _print_chat_status(base, session_id, info)
            except typer.Exit:
                pass
            continue
        if command == "/sessions":
            try:
                _print_sessions(base, token)
            except _client.ClientError as exc:
                _chat_request_error(base, exc)
            continue
        if command == "/history":
            try:
                _print_history(base, token, session_id)
            except _client.ClientError as exc:
                _chat_request_error(base, exc)
            continue
        if command in {"/new", "/resume"}:
            if command == "/resume" and not argument:
                _output.warning("Usage: /resume ID")
                continue
            candidate = argument or _new_session_id()
            if not _valid_session_id(candidate):
                _output.warning(
                    "Invalid session id; use 1-128 letters, digits, dots, underscores, "
                    "colons, or hyphens."
                )
                continue
            session_id = candidate
            verb = "Resuming" if command == "/resume" else "Started"
            _output.info(f"{verb} conversation {session_id}.")
            continue
        if command == "/approve":
            text = "approve"
        elif command == "/deny":
            text = "deny"
        elif command.startswith("/"):
            _output.warning(f"Unknown chat command: {command}")
            _output.hint("Type /help to list available commands.")
            continue

        try:
            hitl = _chat_completion(base, token, session_id, text, stream)
        except _client.ClientError as exc:
            _chat_request_error(base, exc)
            continue
        if hitl:
            _output.hint("Approval required: type /approve to continue or /deny to cancel.")


def _chat_request_error(base: str, exc: _client.ClientError) -> None:
    """Report a failed turn without throwing the operator out of the REPL."""
    if exc.status == 401:
        _output.warning("Skipper rejected the configured agent token.")
        _output.hint("Update it securely with: exa config set agent_token")
        return
    _output.warning(f"Skipper request failed: {exc}")
    _output.hint(f"Check the agent at {base}; your session is still active, so you can retry.")


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
