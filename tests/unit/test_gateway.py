# tests/unit/test_gateway.py
"""B2 — Model gateway & multi-provider routing (ADR 0010, spec B2).

GWT-1 OpenAI-compat call · GWT-2 failover · GWT-3 budget typed error · GWT-4 key scope ·
GWT-5 cost + span · GWT-6 degrade to last-resort.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    get_virtual_key,
    init_db,
    total_gateway_cost,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


def _ok_backend(text="hi"):
    def _b(model, messages, **kw):
        return gw.Completion(
            text=text, model=model, backend="", prompt_tokens=10, completion_tokens=5
        )

    return _b


def _err_backend(model, messages, **kw):
    raise RuntimeError("backend down")


def _router(model="default", backends=None):
    r = gw.Router()
    r.add_route(model, backends or [("primary", _ok_backend())])
    return r


def test_gwt1_openai_compat_chat():
    client = gw.GatewayClient(_router())
    comp = client.chat("default", [{"role": "user", "content": "hello"}])
    assert comp.text == "hi"
    assert comp.backend == "primary"


def test_gwt2_failover_to_fallback():
    r = _router(backends=[("primary", _err_backend), ("fallback", _ok_backend("from-fallback"))])
    comp = gw.GatewayClient(r).chat("default", [{"role": "user", "content": "x"}])
    assert comp.text == "from-fallback"
    assert comp.backend == "fallback"


def test_all_backends_failed_raises():
    r = _router(backends=[("a", _err_backend), ("b", _err_backend)])
    with pytest.raises(gw.AllBackendsFailed):
        gw.GatewayClient(r).chat("default", [{"role": "user", "content": "x"}])


def test_gwt3_budget_typed_error():
    raw = gw.issue_virtual_key("acme", "chat", None, budget_usd=0.0, actor="admin")
    client = gw.GatewayClient(_router(), virtual_key=raw)
    with pytest.raises(gw.BudgetExceeded):
        client.chat("default", [{"role": "user", "content": "x"}])


def test_gwt4_key_scope_rejects_other_model():
    raw = gw.issue_virtual_key("acme", "chat", ["only-this"], None, "admin")
    client = gw.GatewayClient(_router(model="default"), virtual_key=raw)
    with pytest.raises(gw.ModelNotAllowed):
        client.chat("default", [{"role": "user", "content": "x"}])


def test_invalid_key_rejected():
    client = gw.GatewayClient(_router(), virtual_key="exa-nope")
    with pytest.raises(gw.KeyInvalid):
        client.chat("default", [{"role": "user", "content": "x"}])


def test_gwt5_cost_recorded_and_spend_tracked():
    raw = gw.issue_virtual_key("acme", "chat", None, budget_usd=100.0, actor="admin")
    client = gw.GatewayClient(_router(), virtual_key=raw)
    comp = client.chat("default", [{"role": "user", "content": "hello"}])
    assert comp.cost_usd > 0
    assert total_gateway_cost() > 0
    rec = get_virtual_key(gw._hash_key(raw))
    assert rec["spent_usd"] == pytest.approx(comp.cost_usd)


def test_gwt6_degrade_to_last_resort():
    # No route for this model, but a last-resort backend is configured.
    r = gw.Router()  # empty
    client = gw.GatewayClient(r, last_resort=_ok_backend("last-resort-answer"))
    comp = client.chat("unknown-model", [{"role": "user", "content": "x"}])
    assert comp.text == "last-resort-answer"
    assert comp.backend == "last-resort"


def test_semantic_cache_hook_short_circuits():
    calls = {"n": 0}

    def backend(model, messages, **kw):
        calls["n"] += 1
        return gw.Completion(text="fresh", model=model, backend="")

    r = _router(backends=[("primary", backend)])
    client = gw.GatewayClient(r, cache_lookup=lambda m, msgs: "cached-answer")
    comp = client.chat("default", [{"role": "user", "content": "x"}])
    assert comp.cached is True
    assert comp.text == "cached-answer"
    assert calls["n"] == 0  # backend never called on cache hit


def test_issue_key_audited():
    from examlops.platform_db import get_db

    gw.issue_virtual_key("acme", "chat", None, None, "admin")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM audit_events WHERE action='virtual_key_issued'"
        ).fetchall()
    assert len(rows) == 1
