"""OpenAI-compatible chat-completions bridge for ExaMLOps and third-party clients.

`kq` is a general-purpose terminal chat client (session history, full-text
search, branching, and token/cost tracking). Its
default "kube-q" backend speaks the OpenAI Chat Completions wire format:

    POST /v1/chat/completions   (SSE when stream=true, JSON otherwise)
    GET  /healthz

This module translates the wire format onto the ExaMLOps LangGraph agent. The native
``exa chat`` client is canonical; generic clients can use this compatibility surface.

Design notes
------------
* Conversation state is kept server-side by the LangGraph checkpointer. The
  ``X-Session-ID`` header or ``user`` field is only a client label; the storage
  key is namespaced by the verified principal and server-configured tenant.
* HITL: a LangGraph ``interrupt()`` (write-tool confirmation gate) becomes a
  final chunk carrying ``hitl_required=true`` + a short-lived opaque ``action_id``.
  Resuming requires an explicit typed ``action`` object containing that id and an
  ``approve``/``deny`` decision. Ordinary conversation text can never authorize a write.
* Tool activity is surfaced via the ``ki_event`` side-channel understood by ``exa chat`` and
  retained for compatibility with existing clients.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command
from starlette.background import BackgroundTask

from skipper import config, instrument
from skipper.auth import (
    MISCONFIGURED_DETAIL,
    AgentIdentity,
    auth_misconfigured,
    authenticate_bearer,
    scope_thread_id,
    server_secret,
)
from skipper.turns import TurnBusy, TurnCoordinationUnavailable, TurnLease, acquire_turn

router = APIRouter()

_OBJECT_CHUNK = "chat.completion.chunk"
_OBJECT_FULL = "chat.completion"
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTION_ID = re.compile(r"^act\.1\.([0-9]{1,12})\.([A-Za-z0-9_-]{16,64})\.([A-Za-z0-9_-]{32,64})$")
_EPHEMERAL_ACTION_SECRET = secrets.token_bytes(32)


# ── Auth ──────────────────────────────────────────────────────────────────────


def _check_auth(authorization: str | None) -> AgentIdentity:
    """Resolve the verified bearer credential to its server-owned principal."""
    if auth_misconfigured():
        raise HTTPException(status_code=503, detail=MISCONFIGURED_DETAIL)
    identity = authenticate_bearer(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return identity


# ── SSE helpers ───────────────────────────────────────────────────────────────


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _chunk(
    cid: str,
    model: str,
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    hitl_required: bool | None = None,
    action_id: str | None = None,
    usage: dict | None = None,
) -> dict:
    """Build one OpenAI streaming chunk. hitl_required/action_id sit on the choice."""
    delta: dict[str, Any] = {}
    if content is not None:
        delta = {"role": "assistant", "content": content}
    choice: dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": finish_reason}
    if hitl_required is not None:
        choice["hitl_required"] = hitl_required
    if action_id is not None:
        choice["action_id"] = action_id
    obj: dict[str, Any] = {
        "id": cid,
        "object": _OBJECT_CHUNK,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def _norm_usage(usage_metadata: Mapping[str, Any] | None) -> dict | None:
    """Map LangChain usage_metadata → OpenAI usage shape."""
    if not usage_metadata:
        return None
    return {
        "prompt_tokens": usage_metadata.get("input_tokens", 0),
        "completion_tokens": usage_metadata.get("output_tokens", 0),
        "total_tokens": usage_metadata.get("total_tokens", 0),
    }


# ── Graph plumbing (lazy imports avoid a circular import with server.py) ──────


def _graph_and_extract(*, read_only: bool = False):
    from skipper.server import _extract_text, _get_graph, _get_readonly_graph

    return (_get_readonly_graph() if read_only else _get_graph()), _extract_text


def stream_messages(graph, inp, cfg):
    """One turn's ``(message, metadata)`` pairs, sub-agents included (see ``server``)."""
    from skipper.server import stream_messages as _stream

    return _stream(graph, inp, cfg)


def _pending_interrupt(graph, cfg) -> Any | None:
    try:
        tasks = graph.get_state(cfg).tasks or []
        return next((i for t in tasks for i in getattr(t, "interrupts", [])), None)
    except Exception:
        return None


def _urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _action_secret() -> bytes:
    """Return a server-only deployment secret, or an ephemeral local-development secret."""
    if not (
        config.AGENT_ACTION_SIGNING_KEY
        or config.CONTROL_PLANE_TOKEN
        or config.AGENT_API_KEY
        or config.AGENT_API_KEYS_JSON
    ):
        return _EPHEMERAL_ACTION_SECRET
    return server_secret(b"examlops-hitl-action-v1")


def _interrupt_digest(intr: Any) -> str:
    """Hash the pending action without putting its arguments or summary in the client token."""
    try:
        value = intr.value
    except Exception:
        value = None
    interrupt_id = getattr(intr, "id", "")
    canonical = json.dumps(
        {"id": str(interrupt_id), "value": value},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _action_signature(session_id: str, intr: Any, expires: int, nonce: str) -> str:
    payload = f"1\0{session_id}\0{_interrupt_digest(intr)}\0{expires}\0{nonce}".encode()
    return _urlsafe(hmac.new(_action_secret(), payload, hashlib.sha256).digest())


def _issue_action_id(session_id: str, intr: Any, *, now: float | None = None) -> str:
    """Mint an opaque, expiring token bound to exactly one session interrupt."""
    issued_at = time.time() if now is None else now
    expires = int(issued_at) + max(1, config.AGENT_ACTION_TTL_SECONDS)
    nonce = secrets.token_urlsafe(18)
    signature = _action_signature(session_id, intr, expires, nonce)
    return f"act.1.{expires}.{nonce}.{signature}"


def _first_seen_action(key: str, ttl_s: float) -> bool:
    """Atomically claim an action through the configured cross-replica coordinator."""
    from examlops.coordination import get_coordinator

    return get_coordinator().first_seen(key, ttl_s)


def _consume_action_id(session_id: str, intr: Any, action_id: str) -> None:
    """Validate and atomically consume an action token, rejecting expiry and replay."""
    match = _ACTION_ID.fullmatch(action_id)
    if match is None:
        raise HTTPException(
            status_code=409, detail="Approval action does not match the pending write."
        )
    expires_text, nonce, supplied_signature = match.groups()
    expires = int(expires_text)
    expected = _action_signature(session_id, intr, expires, nonce)
    if not hmac.compare_digest(supplied_signature, expected):
        raise HTTPException(
            status_code=409, detail="Approval action does not match the pending write."
        )

    now = int(time.time())
    if now >= expires:
        raise HTTPException(
            status_code=409, detail="Approval action expired; request the action again."
        )

    action_key = hashlib.sha256(f"{session_id}\0{_interrupt_digest(intr)}".encode()).hexdigest()
    try:
        first = _first_seen_action(f"agent-hitl:{action_key}", expires - now)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Approval coordination is unavailable; no action was taken.",
        ) from exc
    if not first:
        raise HTTPException(status_code=409, detail="Approval action was already used.")


def _request_action(body: dict[str, Any]) -> tuple[str, str] | None:
    """Validate the typed HITL extension in a chat-completions request."""
    raw = body.get("action")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="action must be an object")
    action_id = raw.get("action_id")
    decision = raw.get("decision")
    if not isinstance(action_id, str) or not action_id:
        raise HTTPException(status_code=422, detail="action.action_id must be a non-empty string")
    if decision not in {"approve", "deny"}:
        raise HTTPException(status_code=422, detail="action.decision must be 'approve' or 'deny'")
    return action_id, decision


def _build_input(
    graph,
    cfg,
    text: str,
    system_messages: tuple[str, ...] = (),
    action: tuple[str, str] | None = None,
) -> Any:
    """Build a fresh turn or explicitly resume the exact pending HITL action."""
    intr = _pending_interrupt(graph, cfg)
    if intr is not None:
        if action is None:
            raise HTTPException(
                status_code=409,
                detail="A write is awaiting approval; submit its action_id with approve or deny.",
            )
        action_id, decision = action
        _consume_action_id(cfg["configurable"]["thread_id"], intr, action_id)
        return Command(resume="approve" if decision == "approve" else "deny")
    if action is not None:
        raise HTTPException(status_code=409, detail="Approval action is no longer pending.")
    messages: list[Any] = [SystemMessage(content=item) for item in system_messages]
    messages.append(HumanMessage(content=text))
    return {"messages": messages}


def _content_text(content: Any) -> str:
    """Extract text from an OpenAI string or text-content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _request_messages(body: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Validate a stateful chat turn while preserving client-supplied system context."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=422, detail="messages must be a non-empty array")
    systems: list[str] = []
    latest_user = ""
    for item in messages:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="each message must be an object")
        role = item.get("role")
        text = _content_text(item.get("content"))
        if role == "system" and text.strip():
            systems.append(text)
        elif role == "user" and text.strip():
            latest_user = text
    if not latest_user:
        raise HTTPException(status_code=422, detail="a non-empty user message is required")
    return latest_user, tuple(systems)


def _request_session_id(header: str | None, body: dict[str, Any]) -> str:
    """Return a bounded checkpoint key suitable for storage and URL reuse."""
    candidate = header or body.get("user") or f"exa-{uuid.uuid4().hex[:8]}"
    if not isinstance(candidate, str) or not _SESSION_ID.fullmatch(candidate):
        raise HTTPException(
            status_code=422,
            detail="session id must be 1-128 letters, digits, dots, underscores, colons, or hyphens",
        )
    return candidate


def _run_graph_collect(graph, cfg, inp, extract_text):
    """Run the graph to completion (blocking). Returns (text, tool_names, usage, interrupt)."""
    text_parts: list[str] = []
    tool_names: list[str] = []
    usage: dict | None = None
    session_id = cfg.get("configurable", {}).get("thread_id", "kq")
    instr = instrument.start(session_id)
    call_args = instrument.ToolCallArgs()
    try:
        for msg, _meta in stream_messages(graph, inp, cfg):
            if isinstance(msg, AIMessageChunk):
                call_args.observe_ai(msg)
                piece = extract_text(msg.content)
                if piece:
                    text_parts.append(piece)
                if getattr(msg, "usage_metadata", None):
                    usage = _norm_usage(msg.usage_metadata)
            elif isinstance(msg, ToolMessage):
                tool_names.append(msg.name or "tool")
                ok, err = instrument.tool_status(msg)
                if instr.observe(
                    msg.name or "tool", args=call_args.args_for(msg), ok=ok, error=err
                ):
                    text_parts.append(f"\n\n⚠️ {instr.abort_message}")
                    break
    finally:
        instr.finish()
    answer = "".join(text_parts)
    if not answer.strip():
        # Defence in depth. A turn that streamed no text but left a finished reply in the
        # checkpoint used to be reported as an empty answer — indistinguishable, to a caller like
        # ``exa eval operator-qa``, from an agent that had nothing to say. Whatever silences the
        # stream, the state is still the source of truth for what the agent actually answered.
        answer = _final_answer_from_state(graph, cfg, extract_text) or answer
    return answer, tool_names, usage, _pending_interrupt(graph, cfg)


def _final_answer_from_state(graph, cfg, extract_text) -> str:
    """The last assistant message in the checkpointed state, or ``""`` if unreadable."""
    try:
        messages = (graph.get_state(cfg).values or {}).get("messages", [])
    except Exception:  # noqa: BLE001 - a missing checkpointer must not break the turn
        return ""
    for msg in reversed(messages):
        if type(msg).__name__.startswith("AI"):
            text = extract_text(getattr(msg, "content", ""))
            if text.strip():
                return text
    return ""


# ── Streaming endpoint ────────────────────────────────────────────────────────


async def _stream_completion(
    session_id: str,
    model: str,
    graph,
    extract_text,
    cfg,
    inp,
    identity: AgentIdentity,
    lease: TurnLease,
):
    lease.claim_by_worker()
    try:
        cid = _completion_id()
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        instr = instrument.start(session_id)
        call_args = instrument.ToolCallArgs()
    except Exception:
        lease.release()
        raise

    def _run() -> None:
        from skipper import scoping

        try:
            with scoping.identity_scope(identity.principal, identity.tenant):
                for msg, _meta in stream_messages(graph, inp, cfg):
                    if isinstance(msg, AIMessageChunk):
                        call_args.observe_ai(msg)
                        piece = extract_text(msg.content)
                        if piece:
                            loop.call_soon_threadsafe(queue.put_nowait, ("token", piece))
                        if getattr(msg, "usage_metadata", None):
                            loop.call_soon_threadsafe(
                                queue.put_nowait, ("usage", _norm_usage(msg.usage_metadata))
                            )
                    elif isinstance(msg, ToolMessage):
                        loop.call_soon_threadsafe(queue.put_nowait, ("tool", msg.name or "tool"))
                        ok, err = instrument.tool_status(msg)
                        if instr.observe(
                            msg.name or "tool", args=call_args.args_for(msg), ok=ok, error=err
                        ):
                            loop.call_soon_threadsafe(
                                queue.put_nowait, ("error", instr.abort_message)
                            )
                            break
        except Exception as exc:  # surface as an error event
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
        finally:
            instr.finish()
            lease.release()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    try:
        fut = loop.run_in_executor(None, _run)
    except Exception:
        lease.release()
        raise

    usage: dict | None = None
    errored: str | None = None
    timed_out = False
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=config.AGENT_STREAM_IDLE_TIMEOUT)
        except TimeoutError:
            # No token/tool for the idle window — a hung LLM or tool. Abort the
            # stream cleanly rather than blocking the client indefinitely.
            errored = f"agent produced no output for {config.AGENT_STREAM_IDLE_TIMEOUT}s"
            timed_out = True
            break
        if event is None:
            break
        kind, payload = event
        if kind == "token":
            yield _sse(_chunk(cid, model, content=payload))
        elif kind == "tool":
            yield _sse(
                {"ki_event": {"type": "tool_call", "tool_name": payload, "message": payload}}
            )
        elif kind == "usage":
            usage = payload
        elif kind == "error":
            errored = payload
    if not timed_out:
        await fut  # on timeout the worker thread may still be blocked; don't join it

    if errored is not None:
        yield _sse({"ki_event": {"type": "error", "message": errored}})

    # HITL: a pending interrupt means a write-tool is waiting for approval.
    intr = _pending_interrupt(graph, {"configurable": {"thread_id": session_id}})
    if intr is not None:
        summary = ""
        try:
            summary = intr.value.get("summary") or intr.value.get("action") or ""
        except Exception:
            summary = ""
        yield _sse(_chunk(cid, model, content=f"\n🛑 Approval required: {summary}\n"))
        yield _sse(
            _chunk(
                cid,
                model,
                finish_reason="stop",
                hitl_required=True,
                action_id=_issue_action_id(session_id, intr),
            )
        )
    else:
        yield _sse(_chunk(cid, model, finish_reason="stop"))

    if usage is not None:
        yield _sse(
            {"id": cid, "object": _OBJECT_CHUNK, "model": model, "choices": [], "usage": usage}
        )
    yield "data: [DONE]\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_session_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    identity = _check_auth(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    metadata = body.get("metadata")
    read_only = bool(isinstance(metadata, dict) and metadata.get("examlops_read_only") is True)
    requested_session_id = _request_session_id(x_session_id, body)
    session_id = scope_thread_id(identity, requested_session_id, read_only=read_only)
    model = body.get("model") or "examlops-agent"
    text, system_messages = _request_messages(body)
    action = _request_action(body)
    graph, extract_text = _graph_and_extract(read_only=read_only)
    cfg = {"configurable": {"thread_id": session_id}}
    try:
        lease = await asyncio.to_thread(acquire_turn, session_id)
    except TurnBusy as exc:
        raise HTTPException(
            status_code=409,
            detail="Another request is already running for this session; wait and retry.",
        ) from exc
    except TurnCoordinationUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent turn coordination is unavailable; no turn was started.",
        ) from exc
    try:
        inp = _build_input(graph, cfg, text, system_messages, action)
    except Exception:
        await asyncio.to_thread(lease.release)
        raise

    if body.get("stream"):
        return StreamingResponse(
            _stream_completion(session_id, model, graph, extract_text, cfg, inp, identity, lease),
            media_type="text/event-stream",
            # Normally the graph worker releases the lease. This covers a disconnect before the
            # response iterator starts; release is idempotent if the worker already finished.
            background=BackgroundTask(lease.release_if_unclaimed),
        )

    # Non-streaming: run to completion and return a single JSON body.
    try:
        from skipper import scoping

        def _collect():
            try:
                with scoping.identity_scope(identity.principal, identity.tenant):
                    return _run_graph_collect(graph, cfg, inp, extract_text)
            finally:
                # A timed-out ``to_thread`` keeps running. Let that actual worker release the
                # session, not the HTTP timeout path, so a second turn cannot overlap it.
                lease.release()

        full_text, tools, usage, intr = await asyncio.wait_for(
            asyncio.to_thread(_collect),
            timeout=config.AGENT_GRAPH_TIMEOUT,
        )
    except TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "agent graph exceeded its execution timeout"}},
        )
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": {"message": str(exc)}})

    hitl = intr is not None
    if hitl:
        try:
            summary = intr.value.get("summary") or intr.value.get("action") or ""
        except Exception:
            summary = ""
        full_text = f"{full_text}\n🛑 Approval required: {summary}".strip()

    choice: dict[str, Any] = {
        "index": 0,
        "message": {"role": "assistant", "content": full_text},
        "finish_reason": "stop",
        "hitl_required": hitl,
    }
    if hitl:
        choice["action_id"] = _issue_action_id(session_id, intr)
    if tools:
        choice["trace"] = [{"kind": "tool", "name": name, "detail": "completed"} for name in tools]
    resp: dict[str, Any] = {
        "id": _completion_id(),
        "object": _OBJECT_FULL,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp
