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
from typing import Any
from urllib.parse import quote, urlencode

import typer

from examlops.cli import _agent_transport, _client, _output
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
    skipped = record.get("backends_skipped")
    if isinstance(skipped, list):
        rows.append(["Rejected first", ", ".join(str(s) for s in skipped)])
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
    # Read once, then test: two `.get("memory")` calls could disagree, and the second one is
    # what gets used — the check would be guarding a value that is no longer the one in hand.
    raw_memory = info.get("memory")
    memory: dict[str, Any] = raw_memory if isinstance(raw_memory, dict) else {}
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


def _chat_completion(
    base: str,
    token: str,
    session_id: str,
    text: str,
    stream: bool,
    *,
    action: _agent_transport.AgentAction | None = None,
) -> str | None:
    """Send one turn and return the opaque id of any write awaiting approval."""
    # The bridge uses the user value as the thread id when X-Session-ID is absent. Keeping it in
    # the JSON body lets the shared HTTP client remain generic.
    body = _agent_transport.build_request(
        text,
        scoped_agent_session(session_id),
        stream=stream,
        action=action,
        metadata={"project": active_project()},
    )
    result = _agent_transport.request_completion(
        base,
        token,
        body,
        timeout=300.0,
        on_event=_print_chat_event if stream else None,
    )
    if stream and result.answer:
        _output.console.print()
    result.require_valid()
    if not stream:
        _output.console.print(result.answer, markup=False, highlight=False)
    return result.action_id if result.hitl_required else None


def _print_chat_event(event: _agent_transport.AgentEvent) -> None:
    if event.kind == "content":
        _output.console.print(event.text, end="", markup=False, highlight=False)
    elif event.kind == "tool":
        _output.console.print(f"[dim]· {event.text}[/dim]")


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
    pending_action_id: str | None = None

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
            pending_action_id = None
            verb = "Resuming" if command == "/resume" else "Started"
            _output.info(f"{verb} conversation {session_id}.")
            continue
        if command == "/approve":
            if pending_action_id is None:
                _output.warning("No write action is awaiting approval.")
                continue
            action = _agent_transport.AgentAction(pending_action_id, "approve")
        elif command == "/deny":
            if pending_action_id is None:
                _output.warning("No write action is awaiting approval.")
                continue
            action = _agent_transport.AgentAction(pending_action_id, "deny")
        elif command.startswith("/"):
            _output.warning(f"Unknown chat command: {command}")
            _output.hint("Type /help to list available commands.")
            continue
        elif pending_action_id is not None:
            _output.warning("A write is awaiting a decision; use /approve or /deny first.")
            continue
        else:
            action = None

        try:
            next_action_id = _chat_completion(base, token, session_id, text, stream, action=action)
        except _client.ClientError as exc:
            _chat_request_error(base, exc)
            continue
        pending_action_id = next_action_id
        if pending_action_id:
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
# exa agent memory — authenticated remote governance, with explicit legacy local mode
# ---------------------------------------------------------------------------
# ADR 0034 (Accepted) makes a governance promise: an operator can *enumerate, export and
# erase* what the agent remembers, and it names `exa agent memory` as the surface. The
# The default path talks to the running agent, so the server can derive ownership from the
# authenticated credential and enforce the same principal + tenant boundary as chat. Direct
# file access remains available only when the operator explicitly asks for ``--local``.

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
    "  exa agent memory delete pref --scope alice\n\n"
    "  [dim]# Inspect an old local store explicitly[/dim]\n"
    "  exa agent memory stats --local"
)

memory_app = typer.Typer(
    help="Govern authenticated, owner-scoped agent memory (ADR 0034)",
    no_args_is_help=True,
    epilog=_MEMORY_EXAMPLES,
    rich_markup_mode="rich",
)


_MEMORY_KINDS = ("proc", "episode", "pref", "kb")


def _local_memory_admin():
    """Import the agent's memory admin for an explicitly requested local operation.

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
                "Local mode must run where the agent is installed. Point at it with "
                "EXAMLOPS_AGENT_DIR=<repo>/platform/services/agent, or omit --local to use "
                "the authenticated remote API."
            ),
        )
        raise typer.Exit(1) from None


def _local_store():
    """Open the legacy local memory store only after ``--local`` was supplied.

    Naming the path is not decoration. The store is a local file (``AGENT_MEMORY_DB``) and
    the agent usually runs somewhere else, so an operator who erases on the wrong host gets
    a success message and keeps the data. Saying which file was touched makes that visible.
    """
    admin = _local_memory_admin()
    try:
        return admin, admin.open_store()
    except Exception as exc:  # sqlite/langgraph both surface here
        _output.error(
            f"Could not open the agent memory store: {exc}",
            hint="Check AGENT_MEMORY_DB (default ./skipper_memory.db) — it is a local file.",
        )
        raise typer.Exit(1) from None


def _remote_memory_get(path: str) -> object:
    cfg = load_config()
    try:
        return _client.get(f"{cfg.agent_url.rstrip('/')}{path}", token=cfg.agent_token)
    except _client.ClientError as exc:
        _output.error(f"Agent memory request failed: {exc}")


def _remote_memory_post(path: str, body: dict) -> object:
    cfg = load_config()
    try:
        return _client.post(
            f"{cfg.agent_url.rstrip('/')}{path}", body, token=cfg.agent_token, timeout=30.0
        )
    except _client.ClientError as exc:
        _output.error(f"Agent memory request failed: {exc}")


def _require_memory_kind(kind: str) -> None:
    if kind not in _MEMORY_KINDS:
        _output.error(
            f"Unknown memory kind {kind!r}.", hint=f"Choose one of: {', '.join(_MEMORY_KINDS)}"
        )


@memory_app.command("stats", epilog=_MEMORY_EXAMPLES)
def memory_stats(
    local: bool = typer.Option(False, "--local", help="Read AGENT_MEMORY_DB on this machine"),
) -> None:
    """Summarise memory owned by the authenticated principal and tenant."""
    if local:
        _admin, store = _local_store()
        from skipper import config as _cfg  # noqa: PLC0415
        from skipper import memory_types

        data: dict[str, Any] = memory_types.stats(store)
        source = _cfg.AGENT_MEMORY_DB
    else:
        response = _remote_memory_get("/api/memory/stats")
        counts = response.get("counts") if isinstance(response, dict) else None
        if not isinstance(counts, dict):
            _output.error("The agent returned invalid memory statistics.")
        data = counts
        source = load_config().agent_url
    if _output.json_mode:
        _output.print_json({"mode": "local" if local else "remote", "source": source, **data})
        return
    rows = [[str(k), str(v)] for k, v in sorted(data.items())]
    _output.print_table(f"Agent memory — {source}", ["Kind", "Items"], rows)


@memory_app.command("list", epilog=_MEMORY_EXAMPLES)
def memory_list(
    kind: str = typer.Argument(..., help="Memory kind: proc | episode | pref | kb"),
    scope: str = typer.Option(None, "--scope", help="Task-class / model / operator scope"),
    limit: int = typer.Option(50, "--limit", help="Maximum items to show"),
    local: bool = typer.Option(False, "--local", help="Read AGENT_MEMORY_DB on this machine"),
) -> None:
    """Enumerate owner-scoped memories of one kind."""
    _require_memory_kind(kind)
    if limit < 1 or limit > 500:
        _output.error("--limit must be between 1 and 500.")
    if local:
        _admin, store = _local_store()
        from skipper import memory_types  # noqa: PLC0415

        items = memory_types.list_kind(store, kind, scope=scope, limit=limit)
        records = [{"key": it.key, "text": it.value.get("text", "")} for it in items]
    else:
        query = urlencode(
            {k: v for k, v in {"scope": scope, "limit": limit}.items() if v is not None}
        )
        response = _remote_memory_get(f"/api/memory/list/{quote(kind)}?{query}")
        raw = response.get("items") if isinstance(response, dict) else None
        if not isinstance(raw, list):
            _output.error("The agent returned an invalid memory list.")
        records = raw
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
    local: bool = typer.Option(False, "--local", help="Read AGENT_MEMORY_DB on this machine"),
) -> None:
    """Export authenticated owner-scoped memory as JSON."""
    if local:
        _admin, store = _local_store()
        from skipper import memory_types  # noqa: PLC0415

        data: dict[str, Any] = memory_types.export_all(store)
    else:
        response = _remote_memory_get("/api/memory/export")
        memories = response.get("memories") if isinstance(response, dict) else None
        if not isinstance(memories, dict):
            _output.error("The agent returned an invalid memory export.")
        data = memories
    # export_all is keyed by memory kind, so len(data) is the number of kinds, not of
    # memories — count what the operator actually asked to see leave the building.
    total = sum(len(v) for v in data.values())
    if out:
        destination = Path(out)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(destination, flags, 0o600)
        except OSError as exc:
            _output.error(f"Could not create private export {destination}: {exc}")
            return
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
            handle.write("\n")
        destination.chmod(0o600)
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
        None, "--operator", help="Local-mode audit actor (remote mode uses verified principal)"
    ),
    local: bool = typer.Option(False, "--local", help="Erase AGENT_MEMORY_DB on this machine"),
) -> None:
    """Erase memories, cascading to derived ones. Audited to ``audit_events``.

    The immutable audit log is a separate store and is deliberately *not* erased — ADR 0034
    keeps the record that an erasure happened while removing what was remembered.
    """
    _require_memory_kind(kind)
    if operator and not local:
        _output.error("--operator is only valid with --local; remote audit identity is verified.")
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
    destination = "the local memory file" if local else load_config().agent_url
    if not _output.confirm(f"Erase {target} from {destination}? This cannot be undone."):
        _output.detail("Nothing erased.")
        return
    if local:
        _admin, store = _local_store()
        from skipper import config as _cfg  # noqa: PLC0415
        from skipper import memory_types

        n: int | None = memory_types.erase(
            store, kind, scope=scope, operator=operator or _cfg.AGENT_ACTOR
        )
    else:
        response = _remote_memory_post(
            "/api/memory/delete",
            {"kind": kind, "scope": scope, "confirmation": "erase-owned-memory"},
        )
        n = response.get("erased") if isinstance(response, dict) else None
        if not isinstance(n, int):
            _output.error("The agent returned an invalid memory deletion result.")
    if _output.json_mode:
        _output.print_json(
            {"erased": n, "kind": kind, "scope": scope, "mode": "local" if local else "remote"}
        )
        return
    _output.ok(f"Erased {n} {kind} memory item(s)" + (f" for {scope}" if scope else ""))


review_app = typer.Typer(help="List, approve, or reject queued procedure memories")


@review_app.command("list")
def memory_review_list(
    local: bool = typer.Option(False, "--local", help="Read the local review database"),
) -> None:
    """List pending procedure reviews for the authenticated owner."""
    if local:
        _local_memory_admin()
        from skipper import memory_review  # noqa: PLC0415

        reviews: list[dict[str, Any]] | None = memory_review.list_pending()
    else:
        response = _remote_memory_get("/api/memory/reviews")
        reviews = response.get("reviews") if isinstance(response, dict) else None
        if not isinstance(reviews, list):
            _output.error("The agent returned an invalid memory review list.")
    _output.print_json(reviews)


@review_app.command("approve")
def memory_review_approve(
    review_id: int = typer.Argument(..., help="Pending review ID"),
    local: bool = typer.Option(False, "--local", help="Update the local review database"),
) -> None:
    """Approve one queued procedure memory."""
    if local:
        _admin, store = _local_store()
        from skipper import memory_review  # noqa: PLC0415

        message = memory_review.approve(review_id, store)
        _output.ok(message)
        return
    response = _remote_memory_post(f"/api/memory/reviews/{review_id}/approve", {})
    if not isinstance(response, dict) or response.get("status") != "approved":
        _output.error("The agent returned an invalid memory review result.")
    _output.ok(f"Approved memory review #{review_id}.")


@review_app.command("reject")
def memory_review_reject(
    review_id: int = typer.Argument(..., help="Pending review ID"),
    reason: str = typer.Option("", "--reason", help="Reason recorded with the rejection"),
    local: bool = typer.Option(False, "--local", help="Update the local review database"),
) -> None:
    """Reject one queued procedure memory."""
    if local:
        _local_memory_admin()
        from skipper import memory_review  # noqa: PLC0415

        _output.ok(memory_review.reject(review_id, reason=reason))
        return
    response = _remote_memory_post(f"/api/memory/reviews/{review_id}/reject", {"reason": reason})
    if not isinstance(response, dict) or response.get("status") != "rejected":
        _output.error("The agent returned an invalid memory review result.")
    _output.ok(f"Rejected memory review #{review_id}.")


memory_app.add_typer(review_app, name="review")
app.add_typer(memory_app, name="memory")


# ADR 0146 - agent versions and aliases, nested under the existing group. Attached here so the
# existing subcommands are untouched; imported at the bottom to keep this module's import order.
from examlops.cli.commands import agent_version_cmd  # noqa: E402

app.add_typer(agent_version_cmd.version_app, name="version")
app.add_typer(agent_version_cmd.alias_app, name="alias")
app.add_typer(agent_version_cmd.runtime_app, name="runtime")  # ADR 0144
