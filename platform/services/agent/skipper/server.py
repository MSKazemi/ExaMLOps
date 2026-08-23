"""FastAPI chat server for the ExaMLOps agent.

Serves a streaming WebSocket chat interface at / and a REST API at /api/*.
Start with:  uvicorn skipper.server:app --port 18004
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from skipper import config, instrument
from skipper.confirm import _is_affirmative
from skipper.graph import build_graph
from skipper.llm import check_backend

app = FastAPI(title="Skipper (ExaMLOps agent)", docs_url=None, redoc_url=None)

_graph: Any = None
_backend_info: dict = {}
_graph_lock = threading.Lock()

# OpenAI-compatible bridge (/v1/chat/completions + /healthz) consumed by the
# kube-q `kq` terminal client. Imported after `app` so its lazy imports of
# `_get_graph`/`_extract_text` resolve without a circular import.
from skipper.oai_compat import router as oai_router  # noqa: E402

app.include_router(oai_router)


def _get_graph():
    global _graph, _backend_info
    # Double-checked locking: concurrent first-hit requests must not each build a
    # graph (which opens the checkpointer DB + LLM client) and clobber the singleton.
    if _graph is None:
        with _graph_lock:
            if _graph is None:
                _backend_info = check_backend()
                _graph = build_graph(model=_backend_info.get("model"))
    return _graph


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            if isinstance(block, dict) and block.get("type") == "text"
            else (block if isinstance(block, str) else "")
            for block in content
        )
    return ""


def stream_messages(graph, inp, cfg):
    """Yield ``(message, metadata)`` for one turn, including messages produced inside sub-agents.

    ``stream_mode="messages"`` on its own stops at the top-level graph. Since the supervisor
    topology (ADR 0099) moved the work into specialist **subgraphs**, the assistant's own tokens
    are all produced one level down: a turn streams its ``ToolMessage``s and **not a single**
    ``AIMessageChunk``, so a caller collecting streamed text ends up with an empty answer while the
    finished reply sits in the checkpointed state. Measured 2026-08-23 on one question: 0 AI chunks
    without ``subgraphs=True``, 503 (417 carrying text) with it.

    With ``subgraphs=True`` an item is ``(namespace, (message, metadata))`` instead of
    ``(message, metadata)``; both shapes are unwrapped here so callers see one shape.
    """
    for item in graph.stream(inp, cfg, stream_mode="messages", subgraphs=True):
        payload = item[-1] if isinstance(item, tuple) and isinstance(item[-1], tuple) else item
        if isinstance(payload, tuple) and len(payload) == 2:
            yield payload


# ── REST endpoints ────────────────────────────────────────────────────────────


@app.get("/api/info")
async def api_info():
    info = check_backend()
    # `active` is the honest answer to "does this agent remember anything across threads?" — the
    # compiled graph either got a store or it did not. The rest is what was *asked* for, so a
    # mismatch (enabled but not active) points straight at the embedding backend.
    from skipper import memory as _memory

    mem = _memory.status()
    try:
        mem["active"] = getattr(_get_graph(), "store", None) is not None
    except Exception:  # noqa: BLE001 - never let a status field break the info endpoint
        mem["active"] = False
    return {"backend": info["type"], "model": info["model"], "ok": info["ok"], "memory": mem}


@app.get("/api/threads")
async def list_threads():
    graph = _get_graph()
    try:
        seen = list({c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)})
    except Exception:
        seen = []
    return {"threads": sorted(seen)}


@app.get("/api/threads/{thread_id}/history")
async def thread_history(thread_id: str):
    graph = _get_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        state = graph.get_state(cfg)
        messages = state.values.get("messages", [])
        result = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                role = "human"
            elif isinstance(msg, (AIMessage, AIMessageChunk)):
                role = "ai"
            elif isinstance(msg, ToolMessage):
                role = "tool"
            else:
                role = "unknown"
            result.append(
                {
                    "role": role,
                    "content": _extract_text(msg.content) if hasattr(msg, "content") else "",
                    "name": getattr(msg, "name", None),
                }
            )
        return {"messages": result}
    except Exception:
        return {"messages": []}


# ── WebSocket streaming chat ──────────────────────────────────────────────────


@app.websocket("/ws/chat/{thread_id}")
async def chat_websocket(websocket: WebSocket, thread_id: str):
    await websocket.accept()
    graph = _get_graph()
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "message":
                inp: Any = {"messages": [HumanMessage(content=data.get("text", ""))]}
                await _stream_response(websocket, graph, thread_id, inp)
            elif msg_type == "resume":
                answer = data.get("answer", "no")
                inp = Command(resume=answer if _is_affirmative(answer) else "no")
                await _stream_response(websocket, graph, thread_id, inp)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


async def _stream_response(websocket: WebSocket, graph, thread_id: str, inp: Any) -> None:
    cfg = {"configurable": {"thread_id": thread_id}}
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    instr = instrument.start(thread_id)
    call_args = instrument.ToolCallArgs()

    def _run() -> None:
        try:
            for msg, _meta in stream_messages(graph, inp, cfg):
                if isinstance(msg, AIMessageChunk):
                    call_args.observe_ai(msg)
                    text = _extract_text(msg.content)
                    if text:
                        loop.call_soon_threadsafe(queue.put_nowait, {"type": "token", "text": text})
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, {"type": "usage", "usage": usage}
                        )
                elif isinstance(msg, ToolMessage):
                    loop.call_soon_threadsafe(queue.put_nowait, {"type": "tool", "name": msg.name})
                    ok, err = instrument.tool_status(msg)
                    if instr.observe(
                        msg.name or "tool", args=call_args.args_for(msg), ok=ok, error=err
                    ):
                        loop.call_soon_threadsafe(
                            queue.put_nowait, {"type": "error", "message": instr.abort_message}
                        )
                        break
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(exc)})
        finally:
            instr.finish()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    fut = loop.run_in_executor(None, _run)

    timed_out = False
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=config.AGENT_STREAM_IDLE_TIMEOUT)
        except TimeoutError:
            # Hung LLM/tool — no output for the idle window. Tell the client and stop
            # rather than blocking the socket forever.
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"agent produced no output for {config.AGENT_STREAM_IDLE_TIMEOUT}s",
                }
            )
            timed_out = True
            break
        if event is None:
            break
        await websocket.send_json(event)

    if not timed_out:
        await fut  # on timeout the worker thread may still be blocked; don't join it

    # Check for pending interrupt
    try:
        tasks = graph.get_state(cfg).tasks or []
        intr = next((i for t in tasks for i in getattr(t, "interrupts", [])), None)
        if intr is not None:
            await websocket.send_json({"type": "interrupt", "payload": intr.value})
            return
    except Exception:
        pass

    await websocket.send_json({"type": "done"})


# ── Chat UI (served as HTML) ──────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    from skipper.chat_html import CHAT_HTML

    return CHAT_HTML
