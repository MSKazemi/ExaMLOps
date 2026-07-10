"""Backend-for-Frontend view endpoints (F8 / ADR 0058).

The dashboard calls these `/api/v1/*` endpoints instead of fanning out to services itself.
Each endpoint composes its sources through :func:`bff.aggregate`, so a slow or down source
yields a partial (``_partial``-tagged) payload rather than a failed page. AuthZ is enforced
here (viewer role), satisfying "authz MUST be enforced in the BFF" (F8 R2 / F15).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any

from auth import require_role
from bff import aggregate
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from realtime import CHANNELS, bus, sse_frame

router = APIRouter(prefix="/v1", tags=["bff"])
_viewer = require_role("viewer")

# How long the SSE generator waits for an event before emitting a keep-alive comment. Also
# how often it re-checks whether the client has disconnected.
_SSE_KEEPALIVE_SECONDS = 15.0


def _platform_db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _query_one(sql: str, params: tuple = ()) -> Any:
    conn = sqlite3.connect(_platform_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


async def _meta_source() -> dict[str, Any]:
    return {"service": "dashboard-bff", "api": "v1"}


# sqlite3 is blocking; run each query in a worker thread so a slow/locked
# platform.db can't stall the event loop (and defeat aggregate()'s per-source
# timeout, which cannot interrupt sync code running on the loop thread).
async def _traffic_source() -> dict[str, Any]:
    row = await asyncio.to_thread(_query_one, "SELECT COUNT(*) AS c FROM traffic_rules")
    return {"models_with_rules": row["c"] if row else 0}


async def _drift_source() -> dict[str, Any]:
    row = await asyncio.to_thread(
        _query_one, "SELECT COUNT(DISTINCT model) AS c FROM drift_snapshots"
    )
    return {"models_tracked": row["c"] if row else 0}


async def _audit_source() -> dict[str, Any]:
    row = await asyncio.to_thread(_query_one, "SELECT COUNT(*) AS c FROM audit_events")
    return {"total_events": row["c"] if row else 0}


# Overridable so tests can inject fakes and exercise the partial-failure path.
OVERVIEW_SOURCES = {
    "meta": _meta_source,
    "traffic": _traffic_source,
    "drift": _drift_source,
    "audit": _audit_source,
}


@router.get("/overview")
async def overview(_=Depends(_viewer)) -> dict[str, Any]:
    """View-shaped platform overview, composed with per-source timeouts + partial fallback."""
    return await aggregate(OVERVIEW_SOURCES)


def _parse_channels(channels: str) -> tuple[str, ...]:
    """Turn a ``?channels=job.*,drift.*`` query into subscription glob patterns.

    ``*`` (or empty) means every namespace; a bare namespace like ``job`` is expanded to ``job.*``.
    """
    raw = [c.strip() for c in channels.split(",") if c.strip()]
    if not raw or "*" in raw:
        return tuple(f"{ns}.*" for ns in CHANNELS)
    return tuple(c if ("." in c or "*" in c) else f"{c}.*" for c in raw)


@router.get("/stream")
async def stream(request: Request, channels: str = "*", user=Depends(_viewer)) -> StreamingResponse:
    """Multiplexed SSE gateway (F8 R3–R7).

    Subscribes to the requested typed channels, filtered by the caller's tenant, and streams
    events as they are published. Emits a ``hello`` frame immediately, keep-alive comments while
    idle, and reports how many events were dropped under backpressure on disconnect.
    """
    patterns = _parse_channels(channels)
    # ``user`` is the JWT claims dict from require_role — use .get(), not getattr
    # (getattr on a dict always returns the default, silently disabling the
    # per-tenant event filter and leaking every tenant's events onto the stream).
    tenant = user.get("tenant") if isinstance(user, dict) else getattr(user, "tenant", None)
    sub = bus.subscribe(patterns, tenant=tenant)

    async def gen():
        try:
            yield sse_frame("hello", {"channels": list(patterns)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"  # SSE comment — keeps proxies from closing the conn
                    continue
                yield sse_frame(event.channel, {**event.data, "_dropped": sub.dropped})
        finally:
            bus.unsubscribe(sub)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
