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

import os
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

    **The key set is bounded.** It was not: hits were evicted *within* a key, and the key itself
    was kept forever, so the map grew by one entry per distinct client address and never shrank.
    200 000 addresses cost about 160 MB and were still resident long after their window had
    passed — and `/api/auth/login`, which this limiter protects, is unauthenticated, so an attacker
    rotating IPv6 source addresses could grow the process without ever logging in. A rate limiter
    that can be made to exhaust memory is an amplifier, not a control.

    Two bounds, because they fail differently. Expired keys are swept on a cadence, which is enough
    for the ordinary case of many clients over a long uptime. A hard ``max_keys`` covers the case a
    sweep cannot: a flood arriving faster than the sweep interval. When the cap is reached the
    **least recently seen** keys go first, and a refusal counts as being seen — so a client that
    keeps hammering while rotating source addresses to flush the map keeps its own bucket and stays
    blocked. Ordering by the recorded *hits* instead would evict precisely the callers being
    refused, since a refused request records none.

    The honest limit of a bounded map: a client that goes quiet for long enough to become the
    stalest key can be evicted, and its allowance then starts again. That is inherent — the
    alternative is the unbounded map this replaced — and it costs an attacker a pause longer than
    ``max_keys`` other clients' activity to gain one window's worth of requests. Raise ``max_keys``
    if that trade is wrong for your deployment; the memory is roughly a kilobyte per key.
    """

    #: How many `allow()` calls between sweeps of expired keys. Amortises an O(n) pass.
    SWEEP_EVERY = 1024

    def __init__(self, limit: int = 60, window_seconds: float = 60.0, max_keys: int = 10_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        # Last time this key was *seen*, allowed or refused. Kept apart from `_hits`, which only
        # counts allowed requests: a refused caller appends no hit, so ordering eviction by the
        # hit times alone made the clients being blocked look like the stalest ones in the map.
        # A flood of newer keys would then evict exactly the abuser and hand it a fresh allowance.
        self._seen: dict[str, float] = {}
        self._since_sweep = 0
        self._last_sweep: float | None = None

    def allow(self, key: str, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        self._since_sweep += 1
        # Three triggers, because memory should be released for three different reasons: enough
        # calls have gone by (the busy case), the map is over its cap (the flood case), and a whole
        # window has elapsed since the last sweep (the *quiet* case — a process that saw a burst
        # and then went idle must not hold those keys until 1024 more requests happen to arrive).
        if self._last_sweep is None:
            self._last_sweep = t
        if (
            self._since_sweep >= self.SWEEP_EVERY
            or len(self._hits) > self.max_keys
            or t - self._last_sweep >= self.window
        ):
            self._evict(t)
        self._seen[key] = t
        hits = self._hits[key]
        cutoff = t - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(t)
        return True

    def _evict(self, now: float) -> None:
        """Drop keys whose window has passed, then trim to ``max_keys`` if still over."""
        self._since_sweep = 0
        self._last_sweep = now
        cutoff = now - self.window
        for key in [k for k, seen in self._seen.items() if seen < cutoff]:
            self._hits.pop(key, None)
            del self._seen[key]
        if len(self._hits) <= self.max_keys:
            return
        # Still over: a flood of distinct keys inside one window. Keep the most recently *seen* —
        # which includes the callers currently being refused, since a refusal counts as being
        # seen. The keys dropped are the ones nobody has used for longest.
        ordered = sorted(self._seen.items(), key=lambda kv: kv[1], reverse=True)
        keep = {k for k, _ in ordered[: self.max_keys]}
        self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if k in keep})
        self._seen = {k: v for k, v in self._seen.items() if k in keep}

    def reset(self) -> None:
        self._hits.clear()
        self._seen.clear()
        self._since_sweep = 0
        self._last_sweep = None


def _client_key(request: Request) -> str:
    # X-Forwarded-For is honoured only when DASHBOARD_TRUSTED_PROXY says a reverse proxy
    # sits in front (D13): the header is client-forgeable, so trusting it without a proxy
    # would let any caller pick its own rate bucket. Behind a proxy, ignoring it collapses
    # every user into the proxy's address — one shared bucket. Leftmost entry = the client.
    if os.getenv("DASHBOARD_TRUSTED_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
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
