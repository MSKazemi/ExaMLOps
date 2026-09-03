"""Champion-challenger console (ADR 0024 clause 4) — the comparison operators act on.

The scoreboard, the Welch engine and `exa serve challenger` all existed, and the dashboard had only
a read-only *shadow* router — so "is the challenger better, and is it safe to promote" was CLI-only.

The gating here is deliberately reused rather than invented: **disable** takes `traffic.manage`,
the capability that already governs enabling shadow traffic, and **promote** takes `model.promote`,
which is already a step-up capability. A third name for the same authority would be one more thing
to keep in sync.
"""

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _enable(model="JPCP", version="18"):
    from examlops.champion_challenger import enable_shadow

    enable_shadow(model, version, min_delta=0.0, min_samples=1)


# ── reads ─────────────────────────────────────────────────────────────────────


async def test_a_viewer_can_list_challengers(client, platform_db):
    _enable()
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/challenger", headers=_hdr(token))
    assert r.status_code == 200
    assert any(row["model"] == "JPCP" for row in r.json())


async def test_a_model_with_no_challenger_is_not_an_error(client, platform_db):
    """The console asks for whichever model is selected. "There is no challenger here" is an
    ordinary answer, not an error state."""
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/challenger/NOPE", headers=_hdr(token))
    assert r.status_code == 200
    assert r.json() == {"model": "NOPE", "configured": False}


async def test_the_status_carries_the_numbers_the_decision_rests_on(client, platform_db):
    _enable()
    token = await _login(client, VIEWER_PW)
    body = (await client.get("/api/challenger/JPCP", headers=_hdr(token))).json()
    assert body["configured"] is True
    for key in ("delta", "p_value", "n", "significant", "slo_ok", "slo_reason", "policy_met"):
        assert key in body, f"{key} missing — the operator cannot judge the promotion without it"


# ── writes are gated ──────────────────────────────────────────────────────────


async def test_a_viewer_cannot_promote(client, platform_db):
    _enable()
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/challenger/JPCP/promote", headers=_hdr(token))
    assert r.status_code == 403


async def test_a_viewer_cannot_disable(client, platform_db):
    _enable()
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/challenger/JPCP/disable", headers=_hdr(token))
    assert r.status_code == 403


async def test_an_admin_can_disable_and_it_is_audited(client, platform_db):
    _enable()
    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/challenger/JPCP/disable", headers=_hdr(token))
    assert r.status_code == 200 and r.json()["enabled"] is False

    from examlops.data.audit import export_audit_events

    events = [e for e in export_audit_events() if e["action"] == "challenger_disable"]
    assert events and events[0]["source"] == "dashboard"


# ── the refusal has to explain itself ─────────────────────────────────────────


async def test_a_refused_promotion_returns_the_reason_not_an_error(client, platform_db):
    """The operator needs to see *why* — not significant, too few samples, SLO regression. An
    error status would throw that away and leave the console with nothing to show."""
    from examlops.champion_challenger import enable_shadow

    enable_shadow("JPCP", "18", min_delta=0.5, min_samples=1000)  # unreachable policy
    token = await _login(client, ADMIN_PW)
    r = await client.post("/api/challenger/JPCP/promote", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert body["proposed"] is False
    assert body["reason"]
    assert body["status"] is not None, "the refusal must carry the numbers behind it"


# ── it must not be a second implementation ────────────────────────────────────


def test_the_router_uses_the_shared_code_paths_not_raw_sql():
    """Dashboard edit parity: a raw-sqlite mirror of the CLI's logic is how the two surfaces
    drift apart. Reads may fail open over the store, but the *decisions* come from
    `examlops.champion_challenger`."""
    import inspect

    from routers import challenger

    src = inspect.getsource(challenger)
    assert "champion_challenger" in src
    assert "maybe_promote" in src and "challenger_status" in src
    # No hand-rolled scoring, promotion or audit SQL. Checked as SQL fragments rather than the
    # bare word "SELECT" — the first version of this assertion matched the word "selected" in a
    # docstring, which is the same false-positive that makes a guard get switched off.
    for fragment in ("UPDATE challenger_config", "FROM challenger_config", "INSERT INTO audit"):
        assert fragment not in src, f"router re-implements storage: {fragment!r}"
