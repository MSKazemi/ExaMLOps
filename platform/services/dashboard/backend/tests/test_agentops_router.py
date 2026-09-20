"""Agent runs read router (ADR 0021) and the drift-events read (ADR 0022).

Read-only, viewer-gated, tenant-scoped; replay shows the redacted digest and never raw arguments.
"""

import dbconn
import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    from examlops import agentops
    from examlops import platform_db as pdb

    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    agentops.record_session(
        "sess-a",
        "default",
        [
            agentops.AgentStep("recall_memory", args={"q": "secret-arg"}, latency_ms=10),
            agentops.AgentStep("platform_status", ok=False, error="down", latency_ms=30),
        ],
        agent="skipper",
        model="m",
    )
    agentops.record_session(
        "sess-other", "other-tenant", [agentops.AgentStep("recall_memory")], agent="skipper"
    )
    br = agentops.AgentCircuitBreaker(loop_threshold=3, warn_ratio=0.6, session_id="sess-a")
    br.guard(agentops.AgentStep("t", args="a"))
    br.guard(agentops.AgentStep("t", args="a"))
    pdb.record_drift_event("JPCP", "concept", severity="CRITICAL", score=4.2, metric="abs_error")
    pdb.record_drift_event("JPCP", "data_quality", severity="OK", score=0.0)
    return str(db)


async def _hdr(client):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_requires_authentication(client, platform_db):
    assert (await client.get("/api/agentops/sessions")).status_code in (401, 403)


async def test_sessions_are_listed_and_tenant_scoped(client, platform_db):
    r = await client.get("/api/agentops/sessions", headers=await _hdr(client))
    assert r.status_code == 200, r.text
    ids = [s["session_id"] for s in r.json()]
    assert ids == ["sess-a"]  # the other tenant's session is not this viewer's
    assert r.json()[0]["anomalies"] == []


async def test_replay_returns_steps_and_never_the_raw_arguments(client, platform_db):
    r = await client.get("/api/agentops/sessions/sess-a", headers=await _hdr(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["tool"] for s in body["steps"]] == ["recall_memory", "platform_status"]
    assert "secret-arg" not in r.text
    assert len(body["steps"][0]["args_digest"]) == 16


async def test_another_tenants_session_is_indistinguishable_from_a_missing_one(client, platform_db):
    h = await _hdr(client)
    assert (await client.get("/api/agentops/sessions/sess-other", headers=h)).status_code == 404
    assert (await client.get("/api/agentops/sessions/nope", headers=h)).status_code == 404


async def test_tool_success_table(client, platform_db):
    r = await client.get("/api/agentops/tools", headers=await _hdr(client))
    by = {t["tool"]: t for t in r.json()}
    assert by["platform_status"]["success_rate"] == 0.0 and by["platform_status"]["errors"] == 1
    assert by["recall_memory"]["success_rate"] == 1.0


async def test_breaker_warnings_are_listed_without_session_ids(client, platform_db):
    r = await client.get("/api/agentops/breaker", headers=await _hdr(client))
    assert r.status_code == 200
    ev = r.json()
    assert ev and ev[0]["event"] == "warning" and ev[0]["target"] == "loop_warning"
    assert "session_id" not in ev[0]["details"] and "sess-a" not in r.text


async def test_the_router_has_no_write_routes():
    from routers import agentops

    methods = {m for route in agentops.router.routes for m in route.methods}
    assert methods == {"GET"}


async def test_drift_events_filter_by_kind(client, platform_db):
    h = await _hdr(client)
    allr = (await client.get("/api/drift/events", headers=h)).json()
    assert {e["drift_kind"] for e in allr} == {"concept", "data_quality"}
    only = (await client.get("/api/drift/events?kind=concept&model=JPCP", headers=h)).json()
    assert [e["severity"] for e in only] == ["CRITICAL"] and only[0]["metric"] == "abs_error"


async def test_a_failed_read_is_a_503_not_an_all_clear(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "empty-no-schema.db"))
    monkeypatch.setattr(dbconn, "platform_db_path", lambda: str(tmp_path / "missing" / "x.db"))
    r = await client.get("/api/agentops/sessions", headers=await _hdr(client))
    assert r.status_code == 503
