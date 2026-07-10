"""Embedded copilot BFF + endpoint (F11 / ADR 0065)."""

import sqlite3

import copilot
import httpx
import pytest

from tests.conftest import VIEWER_PW

# ── context grounding (R2/R6) ────────────────────────────────────────────────


def test_build_system_context_includes_page_entity_filters_and_untrusted_marker():
    ctx = {"page": "/models/jpcp", "entity": {"name": "jpcp"}, "filters": {"env": "prod"}}
    sysmsg = copilot.build_system_context(ctx)
    assert "/models/jpcp" in sysmsg
    assert "jpcp" in sysmsg and "prod" in sysmsg
    # R6: page context must be explicitly framed as untrusted, never as instructions
    assert "UNTRUSTED" in sysmsg
    assert "MUST NOT execute" in sysmsg


def test_build_system_context_handles_missing_context():
    assert "unknown" in copilot.build_system_context(None)


# ── answer + proposal extraction (R5) ────────────────────────────────────────


def _completion(content, *, hitl=False, trace=None):
    choice = {"message": {"content": content}, "hitl_required": hitl}
    if trace is not None:
        choice["trace"] = trace
    return {"choices": [choice]}


def test_extract_answer_reads_content_and_hitl():
    assert copilot.extract_answer(_completion("hello", hitl=True)) == ("hello", True)


def test_extract_answer_surfaces_error():
    text, hitl = copilot.extract_answer({"error": {"message": "boom"}})
    assert "boom" in text and hitl is False


def test_extract_proposals_flags_mutating_commands():
    answer = "Run `exa drift status` to check, then `exa retrain jpcp --dataset PM100`."
    props = copilot.extract_proposals(answer)
    by_cmd = {p["command"]: p["requiresApproval"] for p in props}
    assert any(c.startswith("exa drift status") for c in by_cmd)
    # read-only command does not require approval
    assert by_cmd[next(c for c in by_cmd if c.startswith("exa drift status"))] is False
    # mutating command requires approval (R5)
    assert by_cmd[next(c for c in by_cmd if c.startswith("exa retrain"))] is True


def test_extract_proposals_dedupes_and_preserves_order():
    props = copilot.extract_proposals("exa status ... again exa status")
    assert len([p for p in props if p["command"] == "exa status"]) == 1


def test_extract_trace_reads_steps():
    data = _completion("hi", trace=[{"kind": "tool", "name": "drift", "detail": "queried jpcp"}])
    trace = copilot.extract_trace(data)
    assert trace[0]["kind"] == "tool" and trace[0]["name"] == "drift"


def test_build_request_body_has_system_then_user():
    body = copilot.build_request_body("why drift?", {"page": "/x"}, session="s")
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["user"] == "s" and body["stream"] is False


def test_parse_response_envelope():
    env = copilot.parse_response(_completion("use `exa retrain m`"))
    assert env["proposals"][0]["requiresApproval"] is True
    assert env["hitl_required"] is False


# ── bridge call with a mocked transport ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_copilot_success_via_mock_transport():
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json=_completion("Try `exa drift status`."))

    out = await copilot.ask_copilot(
        "what's drifting?",
        {"page": "/drift"},
        agent_url="http://agent",
        transport=httpx.MockTransport(handler),
    )
    assert "drift status" in out["answer"]
    assert out["proposals"][0]["requiresApproval"] is False


@pytest.mark.asyncio
async def test_ask_copilot_degrades_gracefully_when_agent_down():
    def handler(request):
        raise httpx.ConnectError("refused")

    out = await copilot.ask_copilot(
        "hi", None, agent_url="http://agent", transport=httpx.MockTransport(handler)
    )
    assert out["_partial"] == ["agent"]
    assert out["proposals"] == [] and "unavailable" in out["answer"].lower()


# ── audit (D4) ───────────────────────────────────────────────────────────────


def test_audit_copilot_writes_event(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, actor TEXT, "
        "action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    assert (
        copilot.audit_copilot(
            str(db), "viewer", "why drift?", {"page": "/drift"}, [{"command": "exa retrain m"}]
        )
        is True
    )
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT source, action, target FROM audit_events").fetchone()
    conn.close()
    assert row == ("dashboard-copilot", "copilot_query", "/drift")


def test_audit_copilot_missing_table_is_noop(tmp_path):
    db = tmp_path / "e.db"
    sqlite3.connect(db).close()
    assert copilot.audit_copilot(str(db), "viewer", "q", None, []) is False


# ── endpoint ──────────────────────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_copilot_endpoint_requires_auth(client):
    r = await client.post("/api/v1/copilot/ask", json={"question": "hi"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_copilot_endpoint_empty_question(client):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/copilot/ask", json={"question": "  "}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200 and r.json()["answer"] == ""


@pytest.mark.asyncio
async def test_copilot_endpoint_degrades_when_agent_unreachable(client, tmp_path, monkeypatch):
    # No agent running in the test env → graceful envelope, never a 500 (still audited).
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, actor TEXT, "
        "action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.setenv("AGENT_URL", "http://127.0.0.1:9")  # nothing listening
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/copilot/ask",
        json={"question": "why is jpcp drifting?", "context": {"page": "/drift"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["_partial"] == ["agent"]
    # audited even on degrade
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='copilot_query'").fetchone()[0]
    conn.close()
    assert n == 1
