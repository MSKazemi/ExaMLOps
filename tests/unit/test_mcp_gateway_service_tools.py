"""MCP surface for the live llm-gateway service (ADR 0147 — agent-operable by contract).

`exa gateway status|providers` (iteration 2 of this program) gave an *operator* a CLI to check the
deployed service; nothing gave an *agent* the same visibility, even though ADR 0147 promises every
capability is agent-callable. These three tools reuse the exact same env-var resolution
(`EXAMLOPS_LLM_GATEWAY_URL`/`AGENT_LLM_GATEWAY_URL`, `LLM_GATEWAY_ADMIN_TOKEN`) as `gateway_cmd.py`,
so the two surfaces can never disagree about which service they mean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli import _client  # noqa: E402
from examlops.mcp import tools  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("EXAMLOPS_LLM_GATEWAY_URL", "AGENT_LLM_GATEWAY_URL", "LLM_GATEWAY_ADMIN_TOKEN"):
        monkeypatch.delenv(var, raising=False)


# ── gateway_service_status ──────────────────────────────────────────────────────


def test_status_reports_the_services_own_ready_document(monkeypatch):
    calls = []

    def fake_get(url, token=""):
        calls.append((url, token))
        return {"ready": True, "routes": {"chat": {"healthy": True, "required": True}}}

    monkeypatch.setattr(tools._client, "get", fake_get)
    res = tools.gateway_service_status()
    assert res["ok"] is True
    assert res["data"]["ready"] is True
    assert calls == [("http://127.0.0.1:18020/ready", "")]  # default URL, no token needed


def test_status_never_raises_for_a_forbidden_configured_url(monkeypatch):
    """Same ADR 0154 egress check as `gateway_service_backend` and `gateway_cmd._gateway_url` —
    an agent-callable tool is the *most* important place to enforce this: a prompt-injected agent
    could be tricked into pointing an env-var-configurable target somewhere it must not reach."""
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    called = []
    monkeypatch.setattr(tools._client, "get", lambda url, token="": called.append(url) or {})
    res = tools.gateway_service_status()
    assert res["ok"] is False and "error" in res
    assert called == []  # refused before any network call, not merely a failed one


def test_status_never_raises_when_the_service_is_unreachable(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    res = tools.gateway_service_status()
    assert res["ok"] is False and "error" in res


def test_status_prefers_examlops_url_then_agent_url_then_the_default(monkeypatch):
    calls = []
    monkeypatch.setattr(tools._client, "get", lambda url, token="": calls.append(url) or {})

    monkeypatch.setenv("AGENT_LLM_GATEWAY_URL", "http://agent-only:8020")
    tools.gateway_service_status()
    assert calls[-1] == "http://agent-only:8020/ready"

    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://cli-specific:8020")
    tools.gateway_service_status()
    assert (
        calls[-1] == "http://cli-specific:8020/ready"
    )  # takes priority over AGENT_LLM_GATEWAY_URL


# ── gateway_service_providers ────────────────────────────────────────────────────


def test_providers_without_an_admin_token_is_a_clean_error_not_a_network_call(monkeypatch):
    called = []
    monkeypatch.setattr(tools._client, "get", lambda url, token="": called.append(url) or {})
    res = tools.gateway_service_providers()
    assert res["ok"] is False and "LLM_GATEWAY_ADMIN_TOKEN" in res["error"]
    assert called == []


def test_providers_sends_the_admin_token_and_reports_provider_health(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "a-real-admin-token")
    calls = []

    def fake_get(url, token=""):
        calls.append((url, token))
        return {"providers": {"n1": {"ok": True, "type": "ollama"}}}

    monkeypatch.setattr(tools._client, "get", fake_get)
    res = tools.gateway_service_providers()
    assert res["ok"] is True and res["data"]["providers"]["n1"]["ok"] is True
    assert calls == [("http://127.0.0.1:18020/admin/health", "a-real-admin-token")]


def test_providers_never_raises_when_the_token_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "wrong")

    def raising_get(url, token=""):
        raise _client.ClientError("HTTP 401")

    monkeypatch.setattr(tools._client, "get", raising_get)
    res = tools.gateway_service_providers()
    assert res["ok"] is False and "error" in res


# ── gateway_service_reload (mutating, write-gated) ───────────────────────────────


def test_reload_respects_a_policy_denial_before_any_network_call(monkeypatch):
    """No `policy.yaml` in this test environment means `_agent_write_gate` itself always allows
    (ADR 0079 decision 2's documented "no file → allow" default) — so a test that only asserts an
    outcome here cannot tell "the gate ran and allowed it" from "the gate was never called".
    Forcing a denial directly is what actually proves the call site is there."""
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "a-real-admin-token")
    monkeypatch.setattr(tools, "_agent_write_gate", lambda *a, **kw: tools._err("policy denied"))
    called = []
    monkeypatch.setattr(tools._client, "post", lambda *a, **kw: called.append(1) or {})
    res = tools.gateway_service_reload()
    assert res["ok"] is False and called == []


def test_reload_without_an_admin_token_is_a_clean_error(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    called = []
    monkeypatch.setattr(tools._client, "post", lambda *a, **kw: called.append(1) or {})
    res = tools.gateway_service_reload()
    assert res["ok"] is False and "LLM_GATEWAY_ADMIN_TOKEN" in res["error"]
    assert called == []


def test_reload_posts_to_the_admin_endpoint_with_the_token(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "a-real-admin-token")
    calls = []

    def fake_post(url, body, token="", **kw):
        calls.append((url, body, token))
        return {"reloaded": True, "routes": ["chat"]}

    monkeypatch.setattr(tools._client, "post", fake_post)
    res = tools.gateway_service_reload()
    assert res["ok"] is True and res["data"]["reloaded"] is True
    assert calls == [("http://127.0.0.1:18020/admin/reload", {}, "a-real-admin-token")]


def test_reload_never_raises_on_a_rejected_config(monkeypatch):
    """A rejected reload is a normal, expected outcome (ADR 0155 d3 keeps the last-good config) —
    not something that should look like a tool crash to the agent."""
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "a-real-admin-token")

    def raising_post(url, body, token="", **kw):
        raise _client.ClientError("HTTP 422")

    monkeypatch.setattr(tools._client, "post", raising_post)
    res = tools.gateway_service_reload()
    assert res["ok"] is False and "error" in res


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_declares_all_three_with_the_right_tiers():
    names = {spec.fn.__name__: spec for spec in tools.REGISTRY}
    assert names["gateway_service_status"].mutating is False
    assert names["gateway_service_providers"].mutating is False
    reload_spec = names["gateway_service_reload"]
    assert reload_spec.mutating is True
    assert reload_spec.tier in {"A", "B", "C"}
    assert reload_spec.annotations["idempotentHint"] is True


def test_reload_is_excluded_when_writes_are_off():
    names = {spec.fn.__name__ for spec in tools.iter_tools(include_writes=False)}
    assert "gateway_service_status" in names
    assert "gateway_service_providers" in names
    assert "gateway_service_reload" not in names


def test_reload_is_included_when_writes_are_on():
    names = {spec.fn.__name__ for spec in tools.iter_tools(include_writes=True)}
    assert "gateway_service_reload" in names
