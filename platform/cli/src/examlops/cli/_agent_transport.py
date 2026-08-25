"""Typed transport for the agent's OpenAI-compatible completion endpoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from examlops.cli import _client

type AgentDecision = Literal["approve", "deny"]
type AgentEventKind = Literal["content", "tool", "error"]


@dataclass(frozen=True, slots=True)
class AgentAction:
    action_id: str
    decision: AgentDecision


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: AgentEventKind
    text: str


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    hitl_required: bool = False
    action_id: str | None = None
    remote_error: str | None = None
    protocol_error: str | None = None

    def require_valid(self) -> AgentResult:
        """Apply the interactive client's strict response contract."""
        if self.remote_error:
            raise _client.ClientError(self.remote_error)
        if self.protocol_error:
            raise _client.ClientError(self.protocol_error)
        if not self.answer.strip() and not self.hitl_required:
            raise _client.ClientError("the agent returned an empty answer")
        if self.hitl_required and not self.action_id:
            raise _client.ClientError("the agent requested approval without an action id")
        return self


def build_request(
    text: str,
    session_id: str,
    *,
    stream: bool,
    action: AgentAction | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared stateful chat-completions request shape."""
    body: dict[str, Any] = {
        "model": "examlops-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
        "user": session_id,
    }
    if metadata is not None:
        body["metadata"] = metadata
    if action is not None:
        body["action"] = {"action_id": action.action_id, "decision": action.decision}
    return body


def request_completion(
    base_url: str,
    token: str,
    body: dict[str, Any],
    *,
    timeout: float,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentResult:
    """Send one completion and normalize JSON or SSE into one typed result."""
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    if not body.get("stream"):
        return parse_completion(_client.post(url, body, token=token, timeout=timeout))

    parts: list[str] = []
    hitl = False
    action_id: str | None = None
    remote_error: str | None = None
    for frame in _client.post_sse(url, body, token=token, timeout=timeout):
        for event in parse_stream_frame(frame):
            if on_event is not None:
                on_event(event)
            if event.kind == "content":
                parts.append(event.text)
            elif event.kind == "error":
                remote_error = event.text or "the agent reported an error"

        if isinstance(frame, dict) and isinstance(frame.get("ki_event"), dict):
            continue
        choice = _first_choice(frame)
        if choice is None:
            continue
        hitl = hitl or bool(choice.get("hitl_required"))
        candidate = choice.get("action_id")
        if isinstance(candidate, str) and candidate:
            action_id = candidate

    return AgentResult(
        answer="".join(parts),
        hitl_required=hitl,
        action_id=action_id,
        remote_error=remote_error,
    )


def parse_completion(data: object) -> AgentResult:
    """Parse one non-streaming OpenAI-style completion defensively."""
    if not isinstance(data, dict):
        return AgentResult(answer="", protocol_error="the agent returned an invalid completion")
    if "error" in data:
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return AgentResult(answer="", remote_error=str(message))
    choice = _first_choice(data)
    if choice is None:
        return AgentResult(
            answer="", protocol_error="the agent returned a completion with no answer"
        )
    message = choice.get("message")
    content = message.get("content", "") if isinstance(message, dict) else ""
    candidate = choice.get("action_id")
    return AgentResult(
        answer=str(content),
        hitl_required=bool(choice.get("hitl_required")),
        action_id=candidate if isinstance(candidate, str) and candidate else None,
    )


def parse_stream_frame(frame: object) -> tuple[AgentEvent, ...]:
    """Translate one SSE frame into display-neutral typed events."""
    if not isinstance(frame, dict):
        return ()
    raw_event = frame.get("ki_event")
    if isinstance(raw_event, dict):
        kind = raw_event.get("type")
        message = str(raw_event.get("message", ""))
        if kind == "tool_call":
            return (AgentEvent("tool", message),)
        if kind == "error":
            return (AgentEvent("error", message),)
        return ()

    choice = _first_choice(frame)
    if choice is None:
        return ()
    delta = choice.get("delta")
    piece = delta.get("content") if isinstance(delta, dict) else None
    return (AgentEvent("content", str(piece)),) if piece else ()


def _first_choice(data: dict[str, Any]) -> dict[str, Any] | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]
