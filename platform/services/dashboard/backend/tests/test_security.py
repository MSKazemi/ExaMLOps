"""Security-hardening baseline: headers middleware + rate limiter (F16 / ADR 0053)."""

import pytest
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
    assert rl.allow("ip", now=5.0) is False   # still inside the 10s window
    assert rl.allow("ip", now=11.0) is True   # first hit aged out


def test_rate_limiter_keys_are_independent():
    rl = RateLimiter(limit=1, window_seconds=100.0)
    assert rl.allow("a", now=1.0)
    assert rl.allow("b", now=1.0)             # different client, own bucket
    assert rl.allow("a", now=1.0) is False


# ── middleware applied to the app (F16 R1) ───────────────────────────────────


@pytest.mark.asyncio
async def test_response_carries_security_headers(client):
    r = await client.get("/api/health")
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
