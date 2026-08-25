"""``exa ask`` — natural-language front door that routes to the Skipper agent.

Turns a plain-English question into an answer by calling the Skipper agent's
OpenAI-compatible chat bridge (``POST /v1/chat/completions``). This is the conversational
entry point to the whole platform: "why is JPCP drifting?", "retrain MACK on PM100", etc.

The agent runs its own tool-calling loop server-side, so ``exa ask`` stays a thin, robust
client. When the agent is unreachable it degrades gracefully with an actionable hint.

Answers stream by default at a terminal. That loop can take a while — the agent may call
several tools before it says anything — and waiting for the whole body first made a slow
answer indistinguishable from a hang. Piped or ``--json`` output does not stream, because
there the whole point is one parseable object.
"""

from __future__ import annotations

import sys
import uuid

import typer

from examlops.cli import _agent_transport, _client, _output
from examlops.cli._config import load_config, scoped_agent_session

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Ask a question in plain English[/dim]\n"
    '  exa ask "which models are drifting and why?"\n\n'
    "  [dim]# Keep context across turns with a session id[/dim]\n"
    '  exa ask "now retrain the worst one" --session mysession\n\n'
    "  [dim]# Approve exactly the pending action returned for that session[/dim]\n"
    "  exa ask --session mysession --approve ACTION_ID\n\n"
    "  [dim]# Machine-readable answer for scripting[/dim]\n"
    '  exa --json ask "list production models"\n\n'
    "  [dim]# Wait for the whole answer instead of streaming it[/dim]\n"
    '  exa ask "summarise last week" --no-stream'
)


def ask(
    question: list[str] | None = typer.Argument(
        None, help="Your question in plain English (quote it or pass as words)"
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Session id to preserve conversational context (default: isolated one-shot)",
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Print the answer as it is generated (default: on at a terminal, off when piped)",
    ),
    approve: str | None = typer.Option(
        None, "--approve", metavar="ACTION_ID", help="Approve one pending action in this session"
    ),
    deny: str | None = typer.Option(
        None, "--deny", metavar="ACTION_ID", help="Deny one pending action in this session"
    ),
) -> None:
    """Ask the Skipper agent a question in natural language."""
    if approve and deny:
        _output.error("Choose only one of --approve or --deny.")
        return
    action_id = approve or deny
    if action_id and not session:
        _output.error("--approve/--deny requires --session so the action cannot cross sessions.")
        return

    text = " ".join(question or []).strip()
    if not text and not action_id:
        _output.error("Empty question.", hint='Try: exa ask "which models are in production?"')
        return

    session = session or f"exa-ask-{uuid.uuid4().hex[:12]}"
    cfg = load_config()
    token = cfg.agent_token
    # Auto: stream at a terminal, not when the caller is going to parse us. `--json` must stay a
    # single object, and a piped consumer generally wants the whole answer at once.
    if stream is None:
        stream = not _output.json_mode and _is_terminal()
    action = (
        _agent_transport.AgentAction(action_id, "approve" if approve else "deny")
        if action_id
        else None
    )
    body = _agent_transport.build_request(
        # The compatibility schema requires a user message even for a typed resume. The server
        # ignores this marker when `action` is present and resumes only the bound interrupt.
        text or "[approval decision]",
        scoped_agent_session(session),
        stream=stream,
        action=action,
    )

    try:
        if stream:
            result = _agent_transport.request_completion(
                cfg.agent_url,
                token,
                body,
                timeout=120.0,
                on_event=_print_stream_event,
            )
            if result.answer:
                _output.console.print()
        else:
            with _output.spinner("Thinking…"):
                result = _agent_transport.request_completion(
                    cfg.agent_url, token, body, timeout=120.0
                )
    except _client.ClientError as exc:
        if exc.status is not None:
            # The agent answered, so it is running — telling the operator to start it sends them
            # down the wrong path. Report what it actually said (usually an upstream LLM failure).
            _output.error(
                f"The Skipper agent at {cfg.agent_url} returned an error: {exc}",
                hint="The agent is up (it answered), so this is usually its LLM backend — "
                "an expired or misconfigured key. Check the agent's own logs.",
            )
        else:
            _output.error(
                f"Could not reach the Skipper agent at {cfg.agent_url}: {exc}",
                hint="Start it with: make skipper-server   "
                "(or set AGENT_URL / exa config set agent <url>)",
            )
        return

    answer = (
        f"[error] {result.remote_error}" if result.remote_error and not stream else result.answer
    )
    hitl = result.hitl_required
    pending_action_id = result.action_id
    if _output.json_mode:
        _output.print_json(
            {
                "answer": answer,
                "hitl_required": hitl,
                "action_id": pending_action_id,
                "session": session,
            }
        )
        return

    if not answer:
        _output.warning("The agent returned an empty answer.")
        return
    if not stream:  # streaming already put the text on screen as it arrived
        _output.console.print(answer)
    if hitl:
        if not pending_action_id:
            _output.error("The agent requested approval without an action ID; refusing to resume.")
            return
        _output.hint("This action needs approval; use the exact one-use action ID below.")
        _output.hint(f"Approve: exa ask --session {session} --approve {pending_action_id}")
        _output.hint(f"Deny:    exa ask --session {session} --deny {pending_action_id}")


def _is_terminal() -> bool:
    """Whether stdout is a terminal, as one call so it can be swapped in a test.

    Reading ``sys.stdout`` at the point of use is correct at runtime but untestable: a CLI test
    harness replaces ``sys.stdout`` *after* a patch would have been applied, so the patch is
    thrown away and the assertion silently tests the default instead.
    """
    return sys.stdout.isatty()


def _print_stream_event(event: _agent_transport.AgentEvent) -> None:
    """Render typed stream events while preserving the one-shot command's UX."""
    if event.kind == "content":
        _output.console.print(event.text, end="", markup=False, highlight=False)
    elif event.kind == "tool":
        _output.console.print(f"[dim]· {event.text}[/dim]")
    elif event.kind == "error":
        _output.console.print(f"[red]· {event.text}[/red]")
