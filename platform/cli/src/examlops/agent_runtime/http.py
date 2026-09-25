"""HTTP surface of the agent runtime (ADR 0144 decision 2).

Mirrors the LangGraph Agent Protocol shape - assistants, threads, runs - so LangGraph-native
clients map onto it; ``assistants`` are the agents and aliases of the snapshot::

    GET    /healthz
    GET    /assistants
    POST   /threads                         {"agent", "alias"?, "metadata"?}
    GET    /threads?status=&after=&limit=
    GET    /threads/{thread_id}
    DELETE /threads/{thread_id}
    GET    /threads/{thread_id}/state
    POST   /threads/{thread_id}/runs        {"input"}                 -> 202, runs in background
    POST   /threads/{thread_id}/runs/wait   {"input"} | {"command": {"resume": ...}}
    GET    /threads/{thread_id}/runs/{run_id}
    POST   /threads/{thread_id}/runs/{run_id}/cancel   {"action": interrupt|rollback|cancel}

**Authentication fails closed.** The default authenticator reads
``EXAMLOPS_AGENT_RUNTIME_TOKENS`` - a JSON map ``{token: {"subject", "tenant"}}``; unset, empty
or containing a placeholder token, every request is refused (503 at startup is not an option
for a library, so each request says why). The caller's **tenant comes from its credential**,
never from the request, and another tenant's thread answers 404. A deployment behind the
platform's federated identity (ADR 0120) passes its own ``authenticate`` callable.

**Affinity.** With peers configured, a request for a thread this worker does not own answers
``421 Misdirected Request`` naming the owner (rendezvous hash of the thread id).

FastAPI is imported lazily (the ``examlops[serving]`` / dashboard dependency set).
"""

# No `from __future__ import annotations`: FastAPI resolves the route parameters' annotations
# (``Request``, imported lazily inside ``create_app``) at definition time.

import hmac
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from examlops.agent_runtime.runtime import AgentRuntime
from examlops.agent_runtime.types import RuntimeRefusal

__all__ = ["Principal", "create_app", "token_authenticator"]

_PLACEHOLDERS = {"changeme", "change-me", "secret", "token", "placeholder", "xxx", "test"}
_MAX_BODY = 512 * 1024


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str


def token_authenticator(
    env: str = "EXAMLOPS_AGENT_RUNTIME_TOKENS",
) -> Callable[[str | None], Principal]:
    """Bearer-token authenticator over a JSON map in ``env``; fails closed."""

    def _load() -> dict[str, Principal]:
        raw = os.getenv(env, "").strip()
        if not raw:
            raise RuntimeRefusal("auth_unconfigured", f"{env} is not set", status=503)
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise RuntimeRefusal("auth_unconfigured", f"{env} is not JSON", status=503) from exc
        if not isinstance(doc, dict):
            raise RuntimeRefusal("auth_unconfigured", f"{env} must be a JSON object", status=503)
        out: dict[str, Principal] = {}
        for tok, who in doc.items():
            if len(tok) < 16 or tok.lower() in _PLACEHOLDERS:
                raise RuntimeRefusal(
                    "auth_unconfigured", f"{env} holds a placeholder or short token", status=503
                )
            # The tenant is the isolation boundary: an entry that does not name one is a
            # misconfiguration, never a member of a shared "default" tenant.
            subject = who.get("subject") if isinstance(who, dict) else None
            tenant = who.get("tenant") if isinstance(who, dict) else None
            if not (isinstance(subject, str) and subject.strip()) or not (
                isinstance(tenant, str) and tenant.strip()
            ):
                raise RuntimeRefusal(
                    "auth_unconfigured",
                    f"{env}: every token must name a non-empty 'subject' and 'tenant'",
                    status=503,
                )
            out[tok] = Principal(subject.strip(), tenant.strip())
        if not out:
            raise RuntimeRefusal("auth_unconfigured", f"{env} is empty", status=503)
        return out

    def authenticate(header: str | None) -> Principal:
        tokens = _load()
        if not header or not header.lower().startswith("bearer "):
            raise RuntimeRefusal("unauthenticated", "a bearer token is required", status=401)
        presented = header.split(" ", 1)[1].strip()
        for tok, who in tokens.items():
            if hmac.compare_digest(tok.encode(), presented.encode()):
                return who
        raise RuntimeRefusal("unauthenticated", "unknown token", status=401)

    return authenticate


def create_app(
    runtime: AgentRuntime,
    *,
    authenticate: Callable[[str | None], Principal] | None = None,
    background_workers: int = 4,
    maintainer: Any = None,
) -> Any:
    """The FastAPI app. ``maintainer`` (a ``service.RuntimeMaintainer``) adds its status -
    snapshot generation and refusals, sweep and recovery counts - to ``/healthz``."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    auth = authenticate or token_authenticator()
    pool = ThreadPoolExecutor(
        max_workers=max(1, background_workers), thread_name_prefix="agent-run"
    )
    app = FastAPI(title="ExaMLOps Agent Runtime", docs_url=None, redoc_url=None)

    @app.exception_handler(RuntimeRefusal)
    async def _refusal(_req: Request, exc: RuntimeRefusal) -> JSONResponse:
        return JSONResponse(exc.as_dict(), status_code=exc.status)

    async def _body(req: Request) -> dict[str, Any]:
        too_large = RuntimeRefusal("body_too_large", f"body exceeds {_MAX_BODY} bytes", status=413)
        declared = req.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY:
            raise too_large
        buf = bytearray()
        async for chunk in req.stream():  # bounded as it arrives, never buffered whole first
            buf += chunk
            if len(buf) > _MAX_BODY:
                raise too_large
        raw = bytes(buf)
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise RuntimeRefusal("bad_json", "body is not JSON", status=400) from exc
        if not isinstance(doc, dict):
            raise RuntimeRefusal("bad_json", "body must be a JSON object", status=400)
        return doc

    def _who(req: Request) -> Principal:
        return auth(req.headers.get("authorization"))

    def _affinity(thread_id: str) -> None:
        if runtime.peers:
            own = runtime.owner(thread_id)
            if own != runtime.worker_id:
                raise RuntimeRefusal("misdirected", f"thread is served by {own}", status=421)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        snap = runtime.snapshot or {}
        out: dict[str, Any] = {
            "ok": runtime.snapshot is not None,
            "worker": runtime.worker_id,
            "snapshot_generation": snap.get("generation"),
        }
        if maintainer is not None:  # unauthenticated: counts and states, never file paths
            status = maintainer.status()
            status["snapshot"].pop("path", None)
            if status["snapshot"].get("last_error"):
                status["snapshot"]["last_error"] = "snapshot refused or missing (see logs)"
            out["maintenance"] = status
        return out

    @app.get("/assistants")
    def assistants(req: Request) -> list[dict[str, Any]]:
        _who(req)
        snap = runtime.snapshot or {}
        return [
            {
                "assistant_id": name,
                "aliases": entry.get("aliases", {}),
                "canary_percent": entry.get("canary_percent", 0),
            }
            for name, entry in sorted((snap.get("agents") or {}).items())
        ]

    @app.post("/threads")
    async def create_thread(req: Request) -> dict[str, Any]:
        who = _who(req)
        body = await _body(req)
        if not isinstance(body.get("agent"), str):
            raise RuntimeRefusal("bad_request", "'agent' is required", status=400)
        return await run_in_threadpool(
            lambda: runtime.open_session(
                body["agent"],
                tenant=who.tenant,
                principal=who.subject,
                alias=str(body.get("alias") or "Production"),
                metadata=body.get("metadata") or {},
            )
        )

    @app.get("/threads")
    def list_threads(
        req: Request, status: str | None = None, after: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        who = _who(req)
        return runtime.store.list_threads(
            tenant=who.tenant, status=status, after=after, limit=max(1, min(limit, 500))
        )

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        return runtime.thread(thread_id, tenant=who.tenant)

    @app.delete("/threads/{thread_id}")
    def close_thread(thread_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        _affinity(thread_id)
        return runtime.close_session(thread_id, tenant=who.tenant)

    @app.get("/threads/{thread_id}/state")
    def thread_state(thread_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        s = runtime.get_state(thread_id, tenant=who.tenant)
        return {
            "thread_id": s.thread_id,
            "values": s.values,
            "next": s.next,
            "checkpoint_id": s.checkpoint_id,
            "agent_version_id": s.agent_version_id,
            "state_schema_version": s.state_schema_version,
        }

    @app.post("/threads/{thread_id}/runs", status_code=202)
    async def create_run(thread_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        _affinity(thread_id)
        body = await _body(req)
        run = await run_in_threadpool(
            lambda: runtime.submit(thread_id, body.get("input"), tenant=who.tenant)
        )
        pool.submit(runtime.execute, run["run_id"])
        return run

    @app.post("/threads/{thread_id}/runs/wait")
    async def run_wait(thread_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        _affinity(thread_id)
        body = await _body(req)
        command = body.get("command")
        if isinstance(command, dict) and "resume" in command:
            return await run_in_threadpool(
                lambda: runtime.resume(
                    thread_id, command["resume"], tenant=who.tenant, by=who.subject
                )
            )
        return await run_in_threadpool(
            lambda: runtime.run_wait(thread_id, body.get("input"), tenant=who.tenant)
        )

    @app.get("/threads/{thread_id}/runs/{run_id}")
    def get_run(thread_id: str, run_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        run = runtime.get_run(run_id, tenant=who.tenant)
        if run["thread_id"] != thread_id:
            raise RuntimeRefusal("not_found", f"no run {run_id}", status=404)
        return run

    @app.post("/threads/{thread_id}/runs/{run_id}/cancel")
    async def cancel_run(thread_id: str, run_id: str, req: Request) -> dict[str, Any]:
        who = _who(req)
        body = await _body(req)
        run = await run_in_threadpool(lambda: runtime.get_run(run_id, tenant=who.tenant))
        if run["thread_id"] != thread_id:
            raise RuntimeRefusal("not_found", f"no run {run_id}", status=404)
        return await run_in_threadpool(
            lambda: runtime.cancel(
                run_id, tenant=who.tenant, action=str(body.get("action") or "interrupt")
            )
        )

    return app
