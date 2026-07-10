"""``exa ask`` — natural-language front door that routes to the Skipper agent.

Turns a plain-English question into an answer by calling the Skipper agent's
OpenAI-compatible chat bridge (``POST /v1/chat/completions``). This is the conversational
entry point to the whole platform: "why is JPCP drifting?", "retrain MACK on PM100", etc.

The agent runs its own tool-calling loop server-side, so ``exa ask`` stays a thin, robust
client. When the agent is unreachable it degrades gracefully with an actionable hint.
"""

from __future__ import annotations

import os

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
    '  exa --json ask "list production models"'
)


def ask(
    question: list[str] = typer.Argument(
        ..., help="Your question in plain English (quote it or pass as words)"
    ),
    session: str = typer.Option(
        "exa-cli", "--session", "-s", help="Session id to preserve conversational context"
    ),
) -> None:
    """Ask the Skipper agent a question in natural language."""
    text = " ".join(question).strip()
    if not text:
        _output.error("Empty question.", hint='Try: exa ask "which models are in production?"')
        return

    cfg = load_config()
    token = os.getenv("AGENT_API_KEY", "")
    body = {
        "model": "examlops-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "user": session,
    }
    url = f"{cfg.agent_url.rstrip('/')}/v1/chat/completions"

    try:
        with _output.spinner("Thinking…"):
            data = _client.post(url, body, token=token, timeout=120.0)
    except _client.ClientError as exc:
        _output.error(
            f"Could not reach the Skipper agent at {cfg.agent_url}: {exc}",
            hint="Start it with: make skipper-server   (or set AGENT_URL / exa config set agent <url>)",
        )
        return

    answer, hitl = _extract_answer(data)

    if _output.json_mode:
        _output.print_json({"answer": answer, "hitl_required": hitl, "session": session})
        return

    if not answer:
        _output.warning("The agent returned an empty answer.")
        return
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
