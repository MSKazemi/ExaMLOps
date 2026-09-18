"""Security-hardening baseline: headers middleware + rate limiter (F16 / ADR 0053)."""

import pytest
import security as sec
from security import RateLimiter, build_csp, security_headers

from tests.conftest import VIEWER_PW

# ── CSP / headers (F16 R1) ───────────────────────────────────────────────────


def test_csp_scopes_frame_ancestors_to_grafana():
    csp = build_csp("http://grafana.example:3000")
    assert "frame-ancestors 'self' http://grafana.example:3000" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp


def test_security_header_set_is_complete():
    hdrs = security_headers("http://g:3000")
    for key in (
        "Content-Security-Policy",
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert key in hdrs
    assert hdrs["X-Content-Type-Options"] == "nosniff"


# ── rate limiter (F16 R7) ────────────────────────────────────────────────────


def test_rate_limiter_allows_up_to_limit_then_blocks():
    rl = RateLimiter(limit=3, window_seconds=100.0)
    # fixed clock so the window never advances
    assert rl.allow("ip", now=1.0)
    assert rl.allow("ip", now=1.0)
    assert rl.allow("ip", now=1.0)
    assert rl.allow("ip", now=1.0) is False  # 4th over the limit of 3


def test_rate_limiter_evicts_old_hits_outside_window():
    rl = RateLimiter(limit=1, window_seconds=10.0)
    assert rl.allow("ip", now=0.0)
    assert rl.allow("ip", now=5.0) is False  # still inside the 10s window
    assert rl.allow("ip", now=11.0) is True  # first hit aged out


def test_rate_limiter_keys_are_independent():
    rl = RateLimiter(limit=1, window_seconds=100.0)
    assert rl.allow("a", now=1.0)
    assert rl.allow("b", now=1.0)  # different client, own bucket
    assert rl.allow("a", now=1.0) is False


# ── middleware applied to the app (F16 R1) ───────────────────────────────────


@pytest.mark.asyncio
async def test_response_carries_security_headers(client):
    """The claim is about the middleware, so the route must not do real work.

    ``/api/health`` fans out to ten backing services, so this header assertion was opening real
    connections to MLflow, Prefect, Ray, Prometheus, Grafana, MinIO, Loki, the control plane and
    the bridge — nine of them, bounded only by an 8-second timeout — and then leaving the result
    in the router's 30-second process-global cache for whatever ran next. Nothing downstream reads
    that cache today, but the middleware is route-independent and none of it was ever the subject.
    """
    from unittest.mock import AsyncMock, patch

    import routers.health as health_router

    health_router._cache.clear()

    with patch("routers.health.httpx.AsyncClient") as client_cls:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        ctx.get = AsyncMock(return_value=AsyncMock(status_code=200))
        client_cls.return_value = ctx
        r = await client.get("/api/health")
    health_router._cache.clear()
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors" in r.headers.get("Content-Security-Policy", "")
    assert "Strict-Transport-Security" in r.headers


# ── search endpoint is rate-limited (F16 R7) ─────────────────────────────────


@pytest.mark.asyncio
async def test_search_endpoint_rate_limited(client, monkeypatch):
    from routers import search as search_router

    # tiny limit so the test provokes a 429 deterministically
    monkeypatch.setattr(search_router._search_limiter, "limit", 2)
    search_router._search_limiter.reset()

    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/search?q=a", headers=hdr)).status_code == 200
    assert (await client.get("/api/v1/search?q=b", headers=hdr)).status_code == 200
    assert (await client.get("/api/v1/search?q=c", headers=hdr)).status_code == 429


# ── client key derivation behind a reverse proxy (D13) ───────────────────────


def _request_with(headers: list[tuple[bytes, bytes]], client_host: str = "10.0.0.9"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (client_host, 12345),
        "query_string": b"",
    }
    return Request(scope)


def test_client_key_ignores_forwarded_for_by_default(monkeypatch):
    from security import _client_key

    monkeypatch.delenv("DASHBOARD_TRUSTED_PROXY", raising=False)
    req = _request_with([(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")])
    # Header is client-forgeable — without a trusted proxy it must not pick the bucket.
    assert _client_key(req) == "10.0.0.9"


def test_client_key_honors_forwarded_for_behind_trusted_proxy(monkeypatch):
    from security import _client_key

    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXY", "1")
    req = _request_with([(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")])
    assert _client_key(req) == "1.2.3.4"


def test_client_key_falls_back_when_proxy_sends_no_header(monkeypatch):
    from security import _client_key

    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXY", "1")
    req = _request_with([])
    assert _client_key(req) == "10.0.0.9"


# ── the limiter's memory is bounded ───────────────────────────────────────────


def test_expired_keys_are_released_not_just_their_hits():
    """Hits were evicted within a key; the key itself was kept forever.

    The map grew by one entry per distinct client address and never shrank — 200 000 addresses cost
    about 160 MB and were still resident long after their windows had passed. `/api/auth/login`,
    which this limiter protects, is unauthenticated, so an attacker rotating IPv6 source addresses
    could grow the dashboard process without ever logging in. A rate limiter that can be made to
    exhaust memory is an amplifier, not a control.
    """
    limiter = sec.RateLimiter(limit=10, window_seconds=60.0, max_keys=10_000)
    start = 1_000.0
    for i in range(5_000):
        limiter.allow(f"2001:db8::{i:x}", now=start)
    assert len(limiter._hits) == 5_000  # all inside the window, all still needed

    # Long after every one of those windows has passed, one unrelated request sweeps them.
    limiter.allow("someone-else", now=start + 10_000)
    assert len(limiter._hits) == 1, (
        f"{len(limiter._hits)} keys survive their own window — the map only ever grows"
    )


def test_a_flood_of_distinct_keys_cannot_grow_the_map_without_bound():
    """A sweep runs on a cadence; a flood can arrive faster than it. The hard cap covers that."""
    limiter = sec.RateLimiter(limit=10, window_seconds=60.0, max_keys=1_000)
    now = 1_000.0
    for i in range(50_000):  # all within one window, so no key has expired
        limiter.allow(f"flood-{i}", now=now)
    assert len(limiter._hits) <= limiter.max_keys + 1, (
        f"{len(limiter._hits)} keys retained against a cap of {limiter.max_keys}"
    )


def test_eviction_never_lets_an_active_abuser_back_in():
    """The property that makes the bound safe rather than a bypass.

    Dropping keys to stay under the cap must not hand an allowance back to the client that is
    actually hammering the endpoint. Least-recently-active goes first, and an abuser's hits are by
    definition the newest.
    """
    limiter = sec.RateLimiter(limit=10, window_seconds=60.0, max_keys=100)
    now = 1_000.0
    allowed = sum(1 for _ in range(25) if limiter.allow("attacker", now=now))
    assert allowed == 10, f"the limiter let {allowed} of 25 through"

    # The realistic shape: the attacker keeps hammering *while* rotating source addresses to flush
    # the map. Its own attempts are refused and therefore record no hit — so ordering eviction by
    # hit times alone made it look like the stalest key in the map, and evicting it handed back a
    # fresh allowance. Refusals now count as activity, which is what keeps its bucket alive.
    for i in range(5_000):
        t = now + 1 + i * 0.001
        limiter.allow(f"noise-{i}", now=t)
        if i % 50 == 0:
            assert not limiter.allow("attacker", now=t), (
                f"the abuser was let back in after {i} flood keys — the cap is a bypass"
            )

    assert not limiter.allow("attacker", now=now + 7)
