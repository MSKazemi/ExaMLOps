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


# ── the datastore is where a request is accounted for, not what answers it ────


def _dead_datastore(monkeypatch):
    import examlops.data.gateway as dg

    def dead(*a, **k):
        raise RuntimeError("could not connect to the datastore")

    monkeypatch.setattr(dg, "init_db", dead)


def test_an_unreachable_datastore_does_not_cost_the_caller_their_answer(monkeypatch, caplog):
    """The backend answered and the tokens were paid for; the accounting write failed afterwards.

    `record_gateway_call` ran unguarded on the request path, so an unreachable datastore raised
    **after** the money was spent and the caller got `could not connect to the datastore` instead
    of the completion they had just bought. Failing the request does not un-spend it, and the spend
    is missing from the ledger either way.
    """
    client = gw.GatewayClient(_router())
    _dead_datastore(monkeypatch)
    before = gw.accounting_failures()

    with caplog.at_level("WARNING"):
        comp = client.chat("default", [{"role": "user", "content": "hello"}])

    assert comp.text == "hi", "the caller lost an answer the backend had already produced"
    # Failing open is the right trade only while it is loud: a budget computed from an incomplete
    # ledger under-reports, and nobody can see that from the number alone.
    assert gw.accounting_failures() > before, "the lost write was not counted"
    assert any("accounting" in r.message and "lost" in r.message for r in caplog.records), (
        f"the loss was silent: {[r.message for r in caplog.records]}"
    )


def test_a_dead_datastore_does_not_replace_the_reason_every_backend_failed(monkeypatch):
    """On the failure path the datastore error stood in for `AllBackendsFailed`.

    The caller then debugged their datastore instead of the backend that actually broke.
    """
    r = gw.Router()
    r.add_route("default", [("primary", _err_backend)])
    client = gw.GatewayClient(r)
    _dead_datastore(monkeypatch)

    with pytest.raises(gw.AllBackendsFailed) as excinfo:
        client.chat("default", [{"role": "user", "content": "x"}])
    assert "backend down" in str(excinfo.value), (
        f"the real cause was replaced by the accounting failure: {excinfo.value}"
    )


def test_authorization_fails_closed_when_the_datastore_is_away(monkeypatch):
    """The other half of the trade above, and the reason it is safe.

    `authorize()` reads the key, its allow-list and its budget from the datastore. With the
    datastore away it refuses **before any backend call**, so nothing is spent — which is why
    accounting may safely fail open afterwards: a keyed request is never served-but-unaccounted by
    an outage, it is refused at the door. Serving a request you cannot authorize would be the worse
    outcome, and it is the one this pins.
    """
    raw = gw.issue_virtual_key("t", "p", None, budget_usd=1.0, actor="me")
    called: list[str] = []

    def _counting_backend(model, messages, **kw):
        called.append(model)
        return gw.Completion(
            text="hi", model=model, backend="", prompt_tokens=1, completion_tokens=1
        )

    r = gw.Router()
    r.add_route("default", [("primary", _counting_backend)])
    client = gw.GatewayClient(r, virtual_key=raw)
    _dead_datastore(monkeypatch)

    with pytest.raises(Exception):
        client.chat("default", [{"role": "user", "content": "x"}])
    assert not called, "the backend was called for a request that could not be authorized"


def test_a_keyless_request_is_still_served_when_the_datastore_is_away(monkeypatch):
    """No key means no `authorize()` read, so this is the case where the gap actually opens:
    the request is served and its usage telemetry is what gets lost."""
    client = gw.GatewayClient(_router())  # no virtual key
    _dead_datastore(monkeypatch)
    before = gw.accounting_failures()

    assert client.chat("default", [{"role": "user", "content": "x"}]).text == "hi"
    assert gw.accounting_failures() > before
