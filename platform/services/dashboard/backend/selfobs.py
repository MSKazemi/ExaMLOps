"""Dashboard self-observability (F24 / ADR 0067).

The dashboard observing *itself*: an in-process metrics collector (request/error/rate-limit counts
+ latency), a BFF-checked dependency-health probe for the in-app status page, and a UI-action audit
helper writing to ``platform_db`` (D4). No third-party egress — everything is self-hosted.

The full feature (GlitchTip error tracking, OTel browser tracing to Tempo, self-hosted analytics,
Playwright synthetics) layers on top; this is the always-available, dependency-free core.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import audit_write
from dbconn import connect, platform_db_path
from starlette.middleware.base import BaseHTTPMiddleware


class Metrics:
    """Process-local counters + a bounded latency window for the dashboard's own requests."""

    def __init__(self, latency_window: int = 200):
        self._latency_window = latency_window
        self.reset()

    def record(self, status_code: int, latency_ms: float) -> None:
        self.requests += 1
        self._latencies.append(latency_ms)
        if status_code == 429:
            self.rate_limit_hits += 1
        if 400 <= status_code < 500:
            self.client_errors += 1
        elif status_code >= 500:
            self.errors += 1

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self._latencies)
        return {
            "requests": self.requests,
            "errors": self.errors,
            "clientErrors": self.client_errors,
            "rateLimitHits": self.rate_limit_hits,
            "latencyMs": {
                "count": len(lat),
                "p50": _percentile(lat, 0.50),
                "p95": _percentile(lat, 0.95),
            },
        }

    def reset(self) -> None:
        self.requests = 0
        self.errors = 0  # 5xx
        self.client_errors = 0  # 4xx
        self.rate_limit_hits = 0  # 429
        self._latencies: deque[float] = deque(maxlen=self._latency_window)


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return round(sorted_vals[idx], 2)


# Process-global collector the middleware feeds and the status endpoint reads.
METRICS = Metrics()


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count / status class / latency for every response (F24 R4)."""

    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        METRICS.record(response.status_code, (time.monotonic() - start) * 1000.0)
        return response


# ── dependency health (F24 R5) ────────────────────────────────────────────────


def _platform_db_path() -> str:
    return platform_db_path()


def dependency_health() -> list[dict[str, Any]]:
    """Health of the dashboard's own dependencies for the in-app status page (F24 R5)."""
    deps: list[dict[str, Any]] = [{"name": "bff", "status": "up"}]
    # platform.db: connectable + queryable?
    start = time.monotonic()
    try:
        conn = connect(_platform_db_path())
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            conn.close()
            deps.append(
                {
                    "name": "platform_db",
                    "status": "up",
                    "latencyMs": round((time.monotonic() - start) * 1000.0, 2),
                }
            )
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        deps.append({"name": "platform_db", "status": "down", "error": str(exc)})
    return deps


def status_payload() -> dict[str, Any]:
    """The in-app status page payload: dependency health + self-metrics (F24 R5)."""
    deps = dependency_health()
    overall = "up" if all(d["status"] == "up" for d in deps) else "degraded"
    return {"status": overall, "dependencies": deps, "metrics": METRICS.snapshot()}


# ── UI-action audit (F24 R4 / D4) ─────────────────────────────────────────────


def record_ui_action(action: str, target: str, actor: str, details: str = "") -> bool:
    """Audit a UI action to ``platform_db.audit_events`` (F24 R4 / D4). Best-effort.

    Returns ``True`` on write, ``False`` when the audit table isn't present (degrades quietly).
    """
    try:
        conn = connect(_platform_db_path())
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
            ).fetchone()
            if not exists:
                return False
            audit_write.audit(
                actor,
                action,
                target,
                {"detail": details} if details else None,
                source="dashboard-ui",
                conn=conn,
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # pragma: no cover - best-effort audit never breaks the request
        return False
