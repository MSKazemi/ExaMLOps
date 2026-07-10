"""Frontend/BFF security-hardening baseline (F16 / ADR 0053).

Two pieces the dashboard app wires in:

* :class:`SecurityHeadersMiddleware` — attaches a strict, static security-header set to every
  response (CSP with ``frame-ancestors`` scoped to the Grafana embed origin [F5], HSTS,
  ``X-Content-Type-Options``, ``Referrer-Policy``, ``Permissions-Policy``) — R1.
* :class:`RateLimiter` + :func:`rate_limit` — a small in-process fixed-window limiter for
  expensive, UI-driven BFF queries; over the limit returns ``429`` — R7.

Deliberately dependency-free and side-effect-light so it is unit-testable without a real ASGI
server or external store. A multi-replica deployment would swap the in-process window for a shared
store (Redis); the dependency seam here stays the same.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


def build_csp(grafana_origin: str) -> str:
    """Strict CSP; only the Grafana embed origin may frame us / be framed (F5/F16 R1).

    Scripts/styles are same-origin (the SPA is served as static assets by this app), images allow
    ``data:`` for inlined icons, and ``frame-ancestors``/``frame-src`` are pinned to the Grafana
    origin so the ``d-solo`` panels (F5) load while nothing else can iframe the dashboard.
    """
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # utility-class styles inject inline <style>
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            f"frame-src 'self' {grafana_origin}",
            f"frame-ancestors 'self' {grafana_origin}",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ]
    )


def security_headers(grafana_origin: str) -> dict[str, str]:
    """The static security-header set applied to every response (F16 R1)."""
    return {
        "Content-Security-Policy": build_csp(grafana_origin),
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "X-Frame-Options": "SAMEORIGIN",
    }


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the security-header set to every response (F16 R1)."""

    def __init__(self, app, grafana_origin: str = "http://localhost:13000"):
        super().__init__(app)
        self._headers = security_headers(grafana_origin)

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for key, value in self._headers.items():
            response.headers.setdefault(key, value)
        return response


class RateLimiter:
    """In-process fixed-window rate limiter keyed by client (F16 R7).

    ``allow(key)`` records a hit and returns ``False`` once more than ``limit`` hits land inside
    the trailing ``window_seconds``. Old hits outside the window are evicted lazily.
    """

    def __init__(self, limit: int = 60, window_seconds: float = 60.0):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = t - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(t)
        return True

    def reset(self) -> None:
        self._hits.clear()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(limiter: RateLimiter):
    """FastAPI dependency factory: 429 when the caller exceeds ``limiter`` (F16 R7)."""

    async def _dep(request: Request) -> None:
        if not limiter.allow(_client_key(request)):
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded — slow down",
                headers={"Retry-After": str(int(limiter.window))},
            )

    return _dep
