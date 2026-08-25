"""FastAPI chat server for the ExaMLOps agent.

Serves a streaming WebSocket chat interface at / and a REST API at /api/*.
Start with:  uvicorn skipper.server:app --port 18004
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from skipper import config, instrument
from skipper.confirm import _is_affirmative
from skipper.graph import build_graph
from skipper.llm import check_backend

app = FastAPI(title="Skipper (ExaMLOps agent)", docs_url=None, redoc_url=None)

_graph: Any = None
_readonly_graph: Any = None
_backend_info: dict = {}
_graph_lock = threading.Lock()

# OpenAI-compatible bridge (/v1/chat/completions + /healthz) used by the native CLI and optional
# third-party clients. Imported after `app` so its lazy imports of
# `_get_graph`/`_extract_text` resolve without a circular import.
from skipper.oai_compat import router as oai_router  # noqa: E402

app.include_router(oai_router)

_AUTH_COOKIE = "examlops_agent_session"


def _token_matches(candidate: str | None) -> bool:
    """Compare an agent credential without leaking a useful timing signal."""
    return bool(
        config.AGENT_API_KEY
        and candidate
        and hmac.compare_digest(str(candidate), config.AGENT_API_KEY)
    )


def _browser_cookie_value() -> str:
    """Derive a session value so the browser never stores the API key itself."""
    return hmac.new(
        config.AGENT_API_KEY.encode(), b"examlops-browser-session-v1", hashlib.sha256
    ).hexdigest()


def _authorized(authorization: str | None, cookie: str | None) -> bool:
    """Authenticate either an API bearer token or the browser's HttpOnly session cookie."""
    if not config.AGENT_API_KEY:
        return True
    scheme, _, credential = (authorization or "").partition(" ")
    bearer = credential if scheme.lower() == "bearer" else None
    cookie_ok = bool(cookie and hmac.compare_digest(cookie, _browser_cookie_value()))
    return _token_matches(bearer) or cookie_ok


def _request_authorized(request: Request) -> bool:
    return _authorized(request.headers.get("authorization"), request.cookies.get(_AUTH_COOKIE))


async def _require_agent_auth(request: Request) -> None:
    if not _request_authorized(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _login_page(*, invalid: bool = False) -> str:
    error = "<p style='color:#f85149'>Invalid API key.</p>" if invalid else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Skipper sign in</title></head>
<body style="font:16px system-ui;max-width:28rem;margin:12vh auto;padding:1rem">
<h1>Skipper</h1><p>Enter the ExaMLOps agent API key to open this browser session.</p>{error}
<form method="post" action="/"><label>API key<br><input name="api_key" type="password"
autocomplete="current-password" required autofocus></label>
<button type="submit">Sign in</button></form></body></html>"""


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


def _get_readonly_graph():
    """Return a graph that cannot call any mutating or durable-memory tool."""
    global _readonly_graph, _backend_info
    if _readonly_graph is None:
        with _graph_lock:
            if _readonly_graph is None:
                _backend_info = check_backend()
                _readonly_graph = build_graph(model=_backend_info.get("model"), read_only=True)
    return _readonly_graph


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
async def api_info(_auth: None = Depends(_require_agent_auth)):
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
    payload = {"backend": info["type"], "model": info["model"], "ok": info["ok"], "memory": mem}
    # `check_backend` already worked out *which* variable is wrong and which candidates it
    # rejected on the way; dropping that here made every client re-derive it from nothing.
    # `exa agent status` is the caller that needs it — it runs on a different machine from the
    # agent, so it cannot inspect the agent's own environment to find out.
    for key in ("fix", "skipped"):
        if info.get(key):
            payload[key] = info[key]
    return payload


@app.get("/api/threads")
async def list_threads(_auth: None = Depends(_require_agent_auth)):
    graph = _get_graph()
    try:
        seen = list({c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)})
    except Exception:
        seen = []
    return {"threads": sorted(seen)}


@app.get("/api/threads/{thread_id}/history")
async def thread_history(thread_id: str, _auth: None = Depends(_require_agent_auth)):
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
    if not _authorized(websocket.headers.get("authorization"), websocket.cookies.get(_AUTH_COOKIE)):
        # Browser WebSocket constructors cannot attach an Authorization header. The built-in UI
        # authenticates through the HttpOnly, same-site cookie issued by POST / below.
        await websocket.close(code=1008, reason="Invalid or missing API key")
        return
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
async def index(request: Request):
    from skipper.chat_html import CHAT_HTML

    if config.AGENT_API_KEY and not _request_authorized(request):
        return HTMLResponse(_login_page())
    return CHAT_HTML


@app.post("/", response_class=HTMLResponse)
async def browser_login(request: Request):
    """Exchange the browser login form for a same-origin cookie usable by fetch and WebSocket.

    The key is submitted in the request body rather than a query string, keeping it out of normal
    access logs. The cookie is HttpOnly and SameSite=Strict; HTTPS deployments also receive the
    Secure attribute.
    """
    if not config.AGENT_API_KEY:
        return RedirectResponse(url="/", status_code=303)
    body = (await request.body())[:8192].decode("utf-8", errors="replace")
    candidate = (parse_qs(body).get("api_key") or [""])[0]
    if not _token_matches(candidate):
        return HTMLResponse(_login_page(invalid=True), status_code=401)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        _AUTH_COOKIE,
        _browser_cookie_value(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response
