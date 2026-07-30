"""OpenAI-compatible chat-completions bridge for the kube-q (`kq`) client.

`kq` is a general-purpose terminal chat client (session history, full-text
search, branching, token/cost tracking, human-in-the-loop approvals). Its
default "kube-q" backend speaks the OpenAI Chat Completions wire format:

    POST /v1/chat/completions   (SSE when stream=true, JSON otherwise)
    GET  /healthz

This module translates that wire format onto the ExaMLOps LangGraph agent so
`kq --url http://localhost:18004` drives the real agent with all its tools — no
fork of kube-q required. Only the tools/prompts are ExaMLOps-specific and those
already live server-side; the chat client stays generic.

Design notes
------------
* Conversation state is kept server-side by the LangGraph SQLite checkpointer,
  keyed by the ``X-Session-ID`` header → ``thread_id``. This matches kube-q's
  ``build_payload`` which sends only the *latest* user message each turn.
* HITL: a LangGraph ``interrupt()`` (write-tool confirmation gate) becomes a
  final chunk carrying ``hitl_required=true`` + ``action_id``. kube-q surfaces
  an approval prompt; typing ``/approve`` sends the literal message ``"approve"``
  (``/deny`` → ``"deny"``), which we route to ``Command(resume=…)``.
* Tool activity is surfaced via the ``ki_event`` side-channel kube-q understands.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from skipper import config, instrument
from skipper.confirm import _is_affirmative

router = APIRouter()

_OBJECT_CHUNK = "chat.completion.chunk"
_OBJECT_FULL = "chat.completion"


# ── Auth ──────────────────────────────────────────────────────────────────────


def _check_auth(authorization: str | None) -> None:
    """Enforce the optional bearer gate. Raises 401 when AGENT_API_KEY is set and unmatched."""
    if not config.AGENT_API_KEY:
        return
    expected = f"Bearer {config.AGENT_API_KEY}"
    # Constant-time compare to avoid leaking the key via response timing.
    if not (authorization and hmac.compare_digest(authorization, expected)):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


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


def _graph_and_extract():
    from skipper.server import _extract_text, _get_graph

    return _get_graph(), _extract_text


def _pending_interrupt(graph, cfg) -> Any | None:
    try:
        tasks = graph.get_state(cfg).tasks or []
        return next((i for t in tasks for i in getattr(t, "interrupts", [])), None)
    except Exception:
        return None


def _build_input(graph, cfg, text: str) -> Any:
    """Route the message: resume a pending HITL interrupt, else a fresh human turn."""
    if _pending_interrupt(graph, cfg) is not None:
        return Command(resume=text if _is_affirmative(text) else "no")
    return {"messages": [HumanMessage(content=text)]}


def _run_graph_collect(graph, cfg, inp, extract_text):
    """Run the graph to completion (blocking). Returns (text, tool_names, usage, interrupt)."""
    text_parts: list[str] = []
    tool_names: list[str] = []
    usage: dict | None = None
    session_id = cfg.get("configurable", {}).get("thread_id", "kq")
    instr = instrument.start(session_id)
    try:
        for item in graph.stream(inp, cfg, stream_mode="messages"):
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            msg, _meta = item
            if isinstance(msg, AIMessageChunk):
                piece = extract_text(msg.content)
                if piece:
                    text_parts.append(piece)
                if getattr(msg, "usage_metadata", None):
                    usage = _norm_usage(msg.usage_metadata)
            elif isinstance(msg, ToolMessage):
                tool_names.append(msg.name or "tool")
                ok, err = instrument.tool_status(msg)
                if instr.observe(msg.name or "tool", ok=ok, error=err):
                    text_parts.append(f"\n\n⚠️ {instr.abort_message}")
                    break
    finally:
        instr.finish()
    return "".join(text_parts), tool_names, usage, _pending_interrupt(graph, cfg)


# ── Streaming endpoint ────────────────────────────────────────────────────────


async def _stream_completion(session_id: str, text: str, model: str):
    graph, extract_text = _graph_and_extract()
    cfg = {"configurable": {"thread_id": session_id}}
    inp = _build_input(graph, cfg, text)
    cid = _completion_id()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    instr = instrument.start(session_id)

    def _run() -> None:
        try:
            for item in graph.stream(inp, cfg, stream_mode="messages"):
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                msg, _meta = item
                if isinstance(msg, AIMessageChunk):
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
                    if instr.observe(msg.name or "tool", ok=ok, error=err):
                        loop.call_soon_threadsafe(queue.put_nowait, ("error", instr.abort_message))
                        break
        except Exception as exc:  # surface as an error event
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
        finally:
            instr.finish()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    fut = loop.run_in_executor(None, _run)

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
                action_id=session_id,
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
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    session_id = x_session_id or body.get("user") or f"kq-{uuid.uuid4().hex[:8]}"
    model = body.get("model") or "examlops-agent"
    messages = body.get("messages") or []
    text = ""
    if messages:
        content = messages[-1].get("content", "")
        text = content if isinstance(content, str) else str(content)

    if body.get("stream"):
        return StreamingResponse(
            _stream_completion(session_id, text, model),
            media_type="text/event-stream",
        )

    # Non-streaming: run to completion and return a single JSON body.
    graph, extract_text = _graph_and_extract()
    cfg = {"configurable": {"thread_id": session_id}}
    inp = _build_input(graph, cfg, text)
    try:
        full_text, _tools, usage, intr = await asyncio.to_thread(
            _run_graph_collect, graph, cfg, inp, extract_text
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
        choice["action_id"] = session_id
    resp: dict[str, Any] = {
        "id": _completion_id(),
        "object": _OBJECT_FULL,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp
