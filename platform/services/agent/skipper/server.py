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

from skipper import config
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


# ── REST endpoints ────────────────────────────────────────────────────────────


@app.get("/api/info")
async def api_info():
    info = check_backend()
    return {"backend": info["type"], "model": info["model"], "ok": info["ok"]}


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

    def _run() -> None:
        try:
            for item in graph.stream(inp, cfg, stream_mode="messages"):
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                msg, _meta = item
                if isinstance(msg, AIMessageChunk):
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
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(exc)})
        finally:
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
