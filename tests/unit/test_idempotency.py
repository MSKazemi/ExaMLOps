"""ADR 0147 d4 — idempotency keys on mutating MCP tools."""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from examlops import idempotency
from examlops.data.idempotency import claim
from examlops.mcp.tools import REGISTRY


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.delenv("EXAMLOPS_IDEMPOTENCY_TTL", raising=False)
    monkeypatch.delenv("EXAMLOPS_IDEMPOTENCY_PENDING_TTL", raising=False)


def _counter():
    calls = []

    @idempotency.idempotent
    def act(model: str, n: int = 1) -> dict:
        calls.append((model, n))
        return {"ok": True, "model": model, "n": n, "call": len(calls)}

    return act, calls


def test_no_key_runs_every_time():
    act, calls = _counter()
    act("a")
    act("a")
    assert len(calls) == 2


def test_replay_returns_original_and_does_not_rerun():
    act, calls = _counter()
    first = act("a", idempotency_key="k1")
    again = act("a", n=1, idempotency_key="k1")  # defaults bind identically
    assert len(calls) == 1
    assert "replayed" not in first
    assert again["replayed"] is True and again["call"] == first["call"]


def test_conflict_when_request_differs_and_not_applied():
    act, calls = _counter()
    act("a", idempotency_key="k1")
    out = act("b", idempotency_key="k1")
    assert out["ok"] is False and out["code"] == "idempotency_conflict"
    assert len(calls) == 1


def test_failed_call_releases_key():
    state = {"n": 0}

    @idempotency.idempotent
    def flaky(x: int) -> dict:
        state["n"] += 1
        return {"ok": state["n"] > 1}

    assert flaky(1, idempotency_key="k")["ok"] is False
    assert flaky(1, idempotency_key="k")["ok"] is True
    assert flaky(1, idempotency_key="k")["replayed"] is True
    assert state["n"] == 2


def test_exception_releases_key():
    @idempotency.idempotent
    def boom(x: int) -> dict:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        boom(1, idempotency_key="k")
    assert claim("k", "boom", "h", 300) is None  # claimable again


def test_expiry_allows_rerun(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_IDEMPOTENCY_TTL", "0.2")
    act, calls = _counter()
    act("a", idempotency_key="k")
    time.sleep(0.35)
    out = act("a", idempotency_key="k")
    assert len(calls) == 2 and "replayed" not in out


def test_stale_pending_claim_expires(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_IDEMPOTENCY_PENDING_TTL", "0.2")
    from examlops.platform_db import init_db

    init_db()
    assert (
        claim("k", "act", "h", idempotency.pending_ttl_seconds()) is None
    )  # holder "crashes" here
    act, calls = _counter()
    out = act("a", idempotency_key="k")
    assert out["code"] == "idempotency_conflict"  # live claim, different request hash
    time.sleep(0.35)
    assert act("a", idempotency_key="k")["ok"] is True
    assert len(calls) == 1


def test_in_flight_is_refused_not_double_run():
    from examlops.platform_db import init_db

    init_db()
    act, calls = _counter()
    digest = idempotency.request_hash("act", {"model": "a", "n": 1})
    assert claim("k", "act", digest, 300) is None  # first call still running
    out = act("a", idempotency_key="k")
    assert out["code"] == "idempotency_in_progress" and not calls


def test_concurrent_double_submit_runs_once():
    started = threading.Barrier(8)
    runs: list[int] = []
    results: list[dict] = []

    @idempotency.idempotent
    def slow(x: int) -> dict:
        runs.append(x)
        time.sleep(0.15)
        return {"ok": True, "x": x}

    from examlops.platform_db import init_db

    init_db()

    def go():
        started.wait()
        results.append(slow(7, idempotency_key="race"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(runs) == 1
    assert sum(1 for r in results if r.get("ok") and not r.get("replayed")) == 1
    assert all(r.get("ok") or r.get("code") == "idempotency_in_progress" for r in results)
    assert slow(7, idempotency_key="race")["replayed"] is True


def test_overlong_key_refused():
    act, calls = _counter()
    assert act("a", idempotency_key="x" * 500)["code"] == "idempotency_invalid"
    assert not calls


def test_wrapper_signature_exposes_parameter():
    act, _ = _counter()
    assert "idempotency_key" in inspect.signature(act).parameters


def test_every_mutating_tool_exposes_idempotency_key():
    from examlops.plans import PLAN_TOOLS

    # plan_change is content-addressed (idempotent by hash) and approve_plan is human-only.
    mutating = [s for s in REGISTRY if s.mutating and s.name not in PLAN_TOOLS - {"apply_plan"}]
    assert mutating
    for spec in mutating:
        assert "idempotency_key" in inspect.signature(spec.fn).parameters, spec.name


def test_real_tool_replay(monkeypatch):
    from examlops.mcp import tools

    calls = []
    monkeypatch.setattr(tools, "_agent_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_audit_write", lambda *a, **k: None)
    import examlops.data.serving as serving

    monkeypatch.setattr(serving, "set_traffic_rules", lambda *a, **k: calls.append(a))
    spec = next(s for s in REGISTRY if s.name == "set_traffic_split")
    a = spec.fn("m", 90, 10, idempotency_key="t1")
    b = spec.fn("m", 90, 10, idempotency_key="t1")
    c = spec.fn("m", 80, 20, idempotency_key="t1")
    assert a["ok"] and b["replayed"] is True and c["code"] == "idempotency_conflict"
    assert len(calls) == 1
