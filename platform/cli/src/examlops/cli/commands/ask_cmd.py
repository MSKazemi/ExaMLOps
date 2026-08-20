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

import os
import sys

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Ask a question in plain English[/dim]\n"
    '  exa ask "which models are drifting and why?"\n\n'
    "  [dim]# Keep context across turns with a session id[/dim]\n"
    '  exa ask "now retrain the worst one" --session mysession\n\n'
    "  [dim]# Machine-readable answer for scripting[/dim]\n"
    '  exa --json ask "list production models"\n\n'
    "  [dim]# Wait for the whole answer instead of streaming it[/dim]\n"
    '  exa ask "summarise last week" --no-stream'
)


def ask(
    question: list[str] = typer.Argument(
        ..., help="Your question in plain English (quote it or pass as words)"
    ),
    session: str = typer.Option(
        "exa-cli", "--session", "-s", help="Session id to preserve conversational context"
    ),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Print the answer as it is generated (default: on at a terminal, off when piped)",
    ),
) -> None:
    """Ask the Skipper agent a question in natural language."""
    text = " ".join(question).strip()
    if not text:
        _output.error("Empty question.", hint='Try: exa ask "which models are in production?"')
        return

    cfg = load_config()
    token = os.getenv("AGENT_API_KEY", "")
    # Auto: stream at a terminal, not when the caller is going to parse us. `--json` must stay a
    # single object, and a piped consumer generally wants the whole answer at once.
    if stream is None:
        stream = not _output.json_mode and _is_terminal()
    body = {
        "model": "examlops-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
        "user": session,
    }
    url = f"{cfg.agent_url.rstrip('/')}/v1/chat/completions"

    try:
        if stream:
            answer, hitl = _stream_answer(url, body, token)
        else:
            with _output.spinner("Thinking…"):
                data = _client.post(url, body, token=token, timeout=120.0)
            answer, hitl = _extract_answer(data)
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

    if _output.json_mode:
        _output.print_json({"answer": answer, "hitl_required": hitl, "session": session})
        return

    if not answer:
        _output.warning("The agent returned an empty answer.")
        return
    if not stream:  # streaming already put the text on screen as it arrived
        _output.console.print(answer)
    if hitl:
        _output.hint(
            "This action needs approval. Reply in the same session to confirm, "
            f'e.g. exa ask "yes" --session {session}'
        )


def _extract_answer(data: object) -> tuple[str, bool]:
    """Pull the assistant text + HITL flag out of an OpenAI-style completion body."""
    if not isinstance(data, dict):
        return "", False
    if "error" in data:
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return f"[error] {msg}", False
    choices = data.get("choices") or []
    if not choices:
        return "", False
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    return str(content), bool(choice.get("hitl_required", False))


def _is_terminal() -> bool:
    """Whether stdout is a terminal, as one call so it can be swapped in a test.

    Reading ``sys.stdout`` at the point of use is correct at runtime but untestable: a CLI test
    harness replaces ``sys.stdout`` *after* a patch would have been applied, so the patch is
    thrown away and the assertion silently tests the default instead.
    """
    return sys.stdout.isatty()


def _stream_answer(url: str, body: dict, token: str) -> tuple[str, bool]:
    """Print an SSE answer as it arrives; return the assembled text and the HITL flag.

    The bridge interleaves two kinds of frame: OpenAI ``choices[0].delta.content`` tokens, and
    its own ``ki_event`` frames announcing a tool call or an error. The tool events are the only
    sign of life during the part of the answer that takes longest — the agent's tool loop before
    it has written a word — so they are shown rather than dropped.
    """
    parts: list[str] = []
    hitl = False
    for frame in _client.post_sse(url, body, token=token, timeout=120.0):
        event = frame.get("ki_event")
        if isinstance(event, dict):
            kind, message = event.get("type"), str(event.get("message", ""))
            if kind == "tool_call":
                _output.console.print(f"[dim]· {message}[/dim]")
            elif kind == "error":
                _output.console.print(f"[red]· {message}[/red]")
            continue
        choices = frame.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if choice.get("hitl_required"):
            hitl = True
        piece = (choice.get("delta") or {}).get("content") or ""
        if piece:
            parts.append(str(piece))
            # markup=False: the answer is model output, and a stray "[" in it is text, not a tag.
            _output.console.print(str(piece), end="", markup=False, highlight=False)
    if parts:
        _output.console.print()
    return "".join(parts), hitl
