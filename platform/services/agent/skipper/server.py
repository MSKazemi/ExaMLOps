"""FastAPI chat server for the ExaMLOps agent.

Serves a streaming WebSocket chat interface at / and a REST API at /api/*.
Start with:  uvicorn skipper.server:app --port 18004
"""

from __future__ import annotations

import asyncio
import hmac
import threading
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from skipper import config, instrument
from skipper.auth import (
    AgentIdentity,
    auth_required,
    authenticate,
    authenticate_key,
    issue_cookie,
    scope_thread_id,
    unscoped_thread_id,
)
from skipper.graph import build_graph
from skipper.llm import check_backend
from skipper.turns import TurnBusy, TurnCoordinationUnavailable, TurnLease, acquire_turn

app = FastAPI(title="Skipper (ExaMLOps agent)", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Keep authenticated conversations and memory responses out of intermediary caches."""

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_graph: Any = None
_readonly_graph: Any = None
_backend_info: dict = {}
_graph_lock = threading.Lock()

# OpenAI-compatible bridge (/v1/chat/completions + /healthz) used by the native CLI and optional
# third-party clients. Imported after `app` so its lazy imports of
# `_get_graph`/`_extract_text` resolve without a circular import.
from skipper.oai_compat import (  # noqa: E402
    _consume_action_id,
    _issue_action_id,
    _pending_interrupt,
)
from skipper.oai_compat import (  # noqa: E402
    router as oai_router,
)

app.include_router(oai_router)

_AUTH_COOKIE = "examlops_agent_session"


def _token_matches(candidate: str | None) -> bool:
    """Compare an agent credential without leaking a useful timing signal."""
    return bool(
        config.AGENT_API_KEY
        and candidate
        and hmac.compare_digest(str(candidate), config.AGENT_API_KEY)
    )


def _authorized(authorization: str | None, cookie: str | None) -> bool:
    """Whether bearer or browser-cookie authentication resolves to a trusted principal."""
    return authenticate(authorization, cookie) is not None


def _identity(authorization: str | None, cookie: str | None) -> AgentIdentity | None:
    return authenticate(authorization, cookie)


def _request_identity(request: Request) -> AgentIdentity | None:
    return _identity(request.headers.get("authorization"), request.cookies.get(_AUTH_COOKIE))


def _request_authorized(request: Request) -> bool:
    return _request_identity(request) is not None


async def _require_agent_auth(request: Request) -> AgentIdentity:
    identity = _request_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return identity


async def _require_memory_auth(request: Request) -> AgentIdentity:
    """Memory administration never falls back to anonymous local-development identity."""
    if not auth_required():
        raise HTTPException(
            status_code=401,
            detail="Memory administration requires a configured agent API credential",
        )
    return await _require_agent_auth(request)


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


class MemoryDeleteRequest(BaseModel):
    kind: str
    scope: str | None = None
    confirmation: str


class MemoryReviewRejectRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


def _memory_store():
    """Return the live graph's memory store without opening a second database handle."""
    store = getattr(_get_graph(), "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Long-term memory is not attached")
    return store


def _memory_scope(identity: AgentIdentity):
    from skipper import scoping

    return scoping.identity_scope(identity.principal, identity.tenant)


# ── REST endpoints ────────────────────────────────────────────────────────────


@app.get("/api/info")
async def api_info(_auth: AgentIdentity = Depends(_require_agent_auth)):
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
async def list_threads(identity: AgentIdentity = Depends(_require_agent_auth)):
    graph = _get_graph()
    try:
        stored = {c.config["configurable"]["thread_id"] for c in graph.checkpointer.list(None)}
        seen = {
            client_id
            for thread_id in stored
            if (client_id := unscoped_thread_id(identity, thread_id)) is not None
        }
    except Exception:
        seen = set()
    return {"threads": sorted(seen)}


@app.get("/api/threads/{thread_id}/history")
async def thread_history(thread_id: str, identity: AgentIdentity = Depends(_require_agent_auth)):
    graph = _get_graph()
    cfg = {"configurable": {"thread_id": scope_thread_id(identity, thread_id)}}
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


# ── Authenticated memory governance ──────────────────────────────────────────


@app.get("/api/memory/stats")
async def memory_stats(identity: AgentIdentity = Depends(_require_memory_auth)):
    from skipper import memory_types

    with _memory_scope(identity):
        counts = memory_types.stats(_memory_store())
    return {"counts": counts}


@app.get("/api/memory/list/{kind}")
async def memory_list(
    kind: str,
    scope: str | None = None,
    limit: int = 50,
    identity: AgentIdentity = Depends(_require_memory_auth),
):
    from skipper import memory_types

    if kind not in memory_types.KINDS:
        raise HTTPException(status_code=422, detail="Unknown memory kind")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with _memory_scope(identity):
        items = memory_types.list_kind(_memory_store(), kind, scope=scope, limit=limit)
    return {"items": [{"key": item.key, "text": item.value.get("text", "")} for item in items]}


@app.get("/api/memory/export")
async def memory_export(identity: AgentIdentity = Depends(_require_memory_auth)):
    from skipper import memory_types

    with _memory_scope(identity):
        data = memory_types.export_all(_memory_store())
    return {"memories": data}


@app.post("/api/memory/delete")
async def memory_delete(
    request: MemoryDeleteRequest,
    identity: AgentIdentity = Depends(_require_memory_auth),
):
    from skipper import memory_types

    if request.kind not in memory_types.KINDS:
        raise HTTPException(status_code=422, detail="Unknown memory kind")
    if request.confirmation != "erase-owned-memory":
        raise HTTPException(
            status_code=409, detail="Explicit memory deletion confirmation required"
        )
    with _memory_scope(identity):
        erased = memory_types.erase(
            _memory_store(), request.kind, scope=request.scope, operator=identity.principal
        )
    return {"erased": erased, "kind": request.kind, "scope": request.scope}


@app.get("/api/memory/reviews")
async def memory_reviews(identity: AgentIdentity = Depends(_require_memory_auth)):
    from skipper import memory_review

    return {
        "reviews": memory_review.list_pending(principal=identity.principal, tenant=identity.tenant)
    }


@app.post("/api/memory/reviews/{review_id}/approve")
async def memory_review_approve(
    review_id: int,
    identity: AgentIdentity = Depends(_require_memory_auth),
):
    from skipper import memory_review

    pending = memory_review.get(review_id, principal=identity.principal, tenant=identity.tenant)
    if pending is None or pending.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Pending memory review not found")
    memory_review.approve(
        review_id,
        _memory_store(),
        reviewer=identity.principal,
        principal=identity.principal,
        tenant=identity.tenant,
    )
    return {"review_id": review_id, "status": "approved"}


@app.post("/api/memory/reviews/{review_id}/reject")
async def memory_review_reject(
    review_id: int,
    request: MemoryReviewRejectRequest,
    identity: AgentIdentity = Depends(_require_memory_auth),
):
    from skipper import memory_review

    pending = memory_review.get(review_id, principal=identity.principal, tenant=identity.tenant)
    if pending is None or pending.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Pending memory review not found")
    memory_review.reject(
        review_id,
        reviewer=identity.principal,
        reason=request.reason,
        principal=identity.principal,
        tenant=identity.tenant,
    )
    return {"review_id": review_id, "status": "rejected"}


# ── WebSocket streaming chat ──────────────────────────────────────────────────


async def _websocket_turn_lease(websocket: WebSocket, thread_id: str) -> TurnLease | None:
    """Acquire one graph turn or send a fail-closed, retryable socket error."""
    try:
        return await asyncio.to_thread(acquire_turn, thread_id)
    except TurnBusy:
        await websocket.send_json(
            {
                "type": "error",
                "code": "session_busy",
                "message": "Another request is already running for this session; wait and retry.",
            }
        )
    except TurnCoordinationUnavailable:
        await websocket.send_json(
            {
                "type": "error",
                "code": "coordination_unavailable",
                "message": "Agent turn coordination is unavailable; no turn was started.",
            }
        )
    return None


@app.websocket("/ws/chat/{thread_id}")
async def chat_websocket(websocket: WebSocket, thread_id: str):
    identity = _identity(
        websocket.headers.get("authorization"), websocket.cookies.get(_AUTH_COOKIE)
    )
    if identity is None:
        # Browser WebSocket constructors cannot attach an Authorization header. The built-in UI
        # authenticates through the HttpOnly, same-site cookie issued by POST / below.
        await websocket.close(code=1008, reason="Invalid or missing API key")
        return
    await websocket.accept()
    graph = _get_graph()
    thread_id = scope_thread_id(identity, thread_id)
    await _send_pending_interrupt(websocket, graph, thread_id)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "message":
                lease = await _websocket_turn_lease(websocket, thread_id)
                if lease is None:
                    continue
                if (
                    _pending_interrupt(graph, {"configurable": {"thread_id": thread_id}})
                    is not None
                ):
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "A write is awaiting approval; use its action controls.",
                            }
                        )
                    finally:
                        await asyncio.to_thread(lease.release)
                    continue
                inp: Any = {"messages": [HumanMessage(content=data.get("text", ""))]}
                await _stream_response(websocket, graph, thread_id, inp, identity, lease)
            elif msg_type == "action":
                lease = await _websocket_turn_lease(websocket, thread_id)
                if lease is None:
                    continue
                cfg = {"configurable": {"thread_id": thread_id}}
                intr = _pending_interrupt(graph, cfg)
                if intr is None:
                    try:
                        await websocket.send_json(
                            {"type": "error", "message": "Approval action is no longer pending."}
                        )
                    finally:
                        await asyncio.to_thread(lease.release)
                    continue
                action_id = data.get("action_id")
                decision = data.get("decision")
                if not isinstance(action_id, str) or decision not in {"approve", "deny"}:
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "A valid action_id and approve/deny decision are required.",
                            }
                        )
                    finally:
                        await asyncio.to_thread(lease.release)
                    continue
                try:
                    _consume_action_id(thread_id, intr, action_id)
                except HTTPException as exc:
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc.detail)})
                    finally:
                        await asyncio.to_thread(lease.release)
                    continue
                inp = Command(resume=decision)
                await _stream_response(websocket, graph, thread_id, inp, identity, lease)
            elif msg_type == "resume":
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Plain-text resume is disabled; use the typed action response.",
                    }
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


async def _stream_response(
    websocket: WebSocket,
    graph,
    thread_id: str,
    inp: Any,
    identity: AgentIdentity,
    lease: TurnLease,
) -> None:
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        instr = instrument.start(thread_id)
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
                        text = _extract_text(msg.content)
                        if text:
                            loop.call_soon_threadsafe(
                                queue.put_nowait, {"type": "token", "text": text}
                            )
                        usage = getattr(msg, "usage_metadata", None)
                        if usage:
                            loop.call_soon_threadsafe(
                                queue.put_nowait, {"type": "usage", "usage": usage}
                            )
                    elif isinstance(msg, ToolMessage):
                        loop.call_soon_threadsafe(
                            queue.put_nowait, {"type": "tool", "name": msg.name}
                        )
                        ok, err = instrument.tool_status(msg)
                        if instr.observe(
                            msg.name or "tool", args=call_args.args_for(msg), ok=ok, error=err
                        ):
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {"type": "error", "message": instr.abort_message},
                            )
                            break
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(exc)})
        finally:
            instr.finish()
            lease.release()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    lease.claim_by_worker()
    try:
        fut = loop.run_in_executor(None, _run)
    except Exception:
        lease.release()
        raise

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

    if await _send_pending_interrupt(websocket, graph, thread_id):
        return

    await websocket.send_json({"type": "done"})


async def _send_pending_interrupt(websocket: WebSocket, graph, thread_id: str) -> bool:
    """Send a typed, expiring approval action when this thread is interrupted."""
    cfg = {"configurable": {"thread_id": thread_id}}
    intr = _pending_interrupt(graph, cfg)
    if intr is None:
        return False
    await websocket.send_json(
        {
            "type": "interrupt",
            "payload": intr.value,
            "action_id": _issue_action_id(thread_id, intr),
        }
    )
    return True


# ── Chat UI (served as HTML) ──────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from skipper.chat_html import CHAT_HTML

    if auth_required() and not _request_authorized(request):
        return HTMLResponse(_login_page())
    return CHAT_HTML


@app.post("/", response_class=HTMLResponse)
async def browser_login(request: Request):
    """Exchange the browser login form for a same-origin cookie usable by fetch and WebSocket.

    The key is submitted in the request body rather than a query string, keeping it out of normal
    access logs. The cookie is HttpOnly and SameSite=Strict; HTTPS deployments also receive the
    Secure attribute.
    """
    if not auth_required():
        return RedirectResponse(url="/", status_code=303)
    body = (await request.body())[:8192].decode("utf-8", errors="replace")
    candidate = (parse_qs(body).get("api_key") or [""])[0]
    identity = authenticate_key(candidate)
    if identity is None:
        return HTMLResponse(_login_page(invalid=True), status_code=401)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        _AUTH_COOKIE,
        issue_cookie(identity),
        httponly=True,
        max_age=max(1, config.AGENT_BROWSER_SESSION_TTL_SECONDS),
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response
