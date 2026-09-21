import types

import httpx
import pytest
from skipper import config, llm


@pytest.fixture(autouse=True)
def _forget_resolved():
    """The probed backend is module state — it must not leak from one test into the next."""
    llm.reset_resolved()
    yield
    llm.reset_resolved()


def _clear_backends(monkeypatch):
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_URL", "")
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture
def probe(monkeypatch):
    """Stub only the httpx name *inside skipper.llm*, not the httpx module.

    These tests must never touch the network: the previous version asserted a
    healthy backend using a fake key against a *real* provider endpoint, and
    passed only because a 401 counted as healthy.

    Patching the global ``httpx.Client`` is not an option — ``openai`` both
    subclasses it at import time and ``isinstance``-checks it at call time, so a
    stubbed class breaks any test that also builds a real LLM object. Rebinding
    ``llm.httpx`` to a shim leaves the real module intact.
    """

    calls: list[tuple[str, dict]] = []
    status = {"code": 200}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            calls.append((url, headers or {}))
            if status["code"] == 0:
                raise httpx.ConnectError("simulated transport failure")
            return _Resp(status["code"])

    monkeypatch.setattr(
        llm,
        "httpx",
        types.SimpleNamespace(Client=lambda *a, **k: _Client(), RequestError=httpx.RequestError),
    )
    return types.SimpleNamespace(calls=calls, status=status)


def test_gateway_backend_preferred_when_configured(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")  # should be overridden
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_URL", "http://gw:8020")
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_KEY", "vk-test")
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_MODEL", "default")

    info = llm.check_backend()
    assert info == {"ok": True, "type": "gateway", "model": "default"}

    built = llm.build_llm()
    # langchain-openai's ChatOpenAI exposes the model id as `model_name`.
    assert built.model_name == "default"


def test_claude_backend_when_no_gateway(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    info = llm.check_backend()
    assert info["type"] == "claude"


def test_the_gateway_is_selected_by_its_url_not_by_its_key(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_KEY", "vk-test")  # url missing
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    # Without a URL there is no gateway to prefer — falls through to Claude.
    assert llm.check_backend()["type"] == "claude"


def _configure_gateway(monkeypatch):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_URL", "http://gw:8020")
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_KEY", "vk-test")
    monkeypatch.setattr(config, "AGENT_LLM_GATEWAY_MODEL", "default")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credential_reports_unhealthy(monkeypatch, probe, status):
    """A live endpoint that refuses the key must NOT report ok:True.

    Regression test for the real LXP outage of 2026-08-20: a hosted endpoint
    was up and answering 401 to a stored key, and check_backend() reported the
    backend healthy — so the agent looked fine while being unable to answer a
    single request. The gateway rejects a bad virtual key the same way.
    """
    _configure_gateway(monkeypatch)
    probe.status["code"] = status

    info = llm.check_backend()
    assert info["ok"] is False
    # The *preferred* backend is the one named: it is the one whose config the operator meant
    # to use, so it is the one worth pointing at.
    assert info["type"] == "gateway"
    assert info["model"] == "default"
    # …and the report says what was tried and which variable to repair, because "not ok" alone
    # is what sent an operator hunting in the wrong place.
    assert info["skipped"] == ["gateway", "ollama"]
    assert "AGENT_LLM_GATEWAY_KEY" in info["fix"]


def test_gateway_probe_sends_the_key(monkeypatch, probe):
    """The probe must authenticate, else it cannot tell a good key from a bad one."""
    _configure_gateway(monkeypatch)

    llm.check_backend()

    url, headers = probe.calls[-1]
    assert url == "http://gw:8020/v1/models", url
    assert headers.get("Authorization") == "Bearer vk-test"
    assert probe.calls[0][0] == "http://gw:8020/ready"  # readiness first, then the key


def test_a_gateway_that_is_not_ready_is_down(monkeypatch, probe):
    """/ready 503 means no model is reachable behind it: it cannot serve, so it is skipped —
    and the fallback (the direct Ollama, break-glass) is reported, never silent."""
    _configure_gateway(monkeypatch)
    probe.status["code"] = 503

    info = llm.check_backend()
    assert info["ok"] is True and info["type"] == "ollama" and info["skipped"] == ["gateway"]


def test_a_latched_ready_gateway_that_is_unhealthy_now_is_treated_as_down(monkeypatch):
    """/ready stays 200 after an outage (it latches, so a pod is not restarted); the body says so."""
    _configure_gateway(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    class _R:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            if "gw:8020" in url:
                return _R(200, {"ready": True, "healthy_now": False, "latched": True})
            return _R(200)

    monkeypatch.setattr(
        llm,
        "httpx",
        types.SimpleNamespace(Client=lambda *a, **k: _Client(), RequestError=httpx.RequestError),
    )
    info = llm.check_backend()
    assert info["type"] == "claude" and info["skipped"] == ["gateway"]


def test_non_auth_status_is_still_reachable(monkeypatch, probe):
    """404/5xx are not credential failures — probe paths differ per provider."""
    _clear_backends(monkeypatch)  # the Ollama probe: /api/tags may 404 on a proxy
    probe.status["code"] = 404

    assert llm.check_backend()["ok"] is True


def test_transport_error_reports_unhealthy(monkeypatch, probe):
    _configure_gateway(monkeypatch)
    probe.status["code"] = 0  # fixture raises httpx.ConnectError

    assert llm.check_backend()["ok"] is False


# ── falling back to a backend that actually works ─────────────────────────────


def _selective_probe(monkeypatch, rejected: set[str]):
    """Probe stub where only the named backends refuse the credential (401)."""
    import types as _t

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            hit = "gateway" if "gw:8020" in url else "claude" if "anthropic" in url else "ollama"
            return _Resp(401 if hit in rejected else 200)

    monkeypatch.setattr(
        llm,
        "httpx",
        _t.SimpleNamespace(Client=lambda *a, **k: _Client(), RequestError=httpx.RequestError),
    )


def test_rejected_preferred_backend_falls_back_to_a_working_one(monkeypatch):
    """One stale key must not take the agent down while a working backend sits behind it.

    This is the 2026-08-20 outage: a hosted backend was configured, its key was rejected, and the
    agent built a client for it anyway that raised AuthenticationError on the first token.
    """
    _configure_gateway(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"gateway"})

    info = llm.check_backend()
    assert info["ok"] is True
    assert info["type"] == "claude"
    # The fallback is reported, never silent: it changes the quality of every answer.
    assert info["skipped"] == ["gateway"]


def test_build_llm_uses_the_backend_that_was_found_working(monkeypatch):
    """The reported backend and the used backend must be the same one."""
    _configure_gateway(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"gateway"})

    llm.check_backend()
    built = llm.build_llm()
    assert type(built).__name__ == "ChatAnthropic"


def test_build_llm_does_no_io_and_keeps_the_preferred_backend_when_unprobed(monkeypatch):
    """Building must not reach the network — probing belongs at the entry points.

    ``build_graph()`` calls ``build_llm()``; if that probed, every graph construction would
    make HTTP calls.
    """
    _configure_gateway(monkeypatch)

    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("build_llm performed I/O")

    monkeypatch.setattr(llm, "_endpoint_reachable", _explode)
    assert type(llm.build_llm()).__name__ == "ChatOpenAI"


def test_a_resolved_backend_that_is_no_longer_configured_is_ignored(monkeypatch):
    """Stale module state must never outrank the current config."""
    _configure_gateway(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"gateway"})
    llm.check_backend()
    assert llm._RESOLVED_TYPE == "claude"

    # The Claude key goes away; the next build must not try to use it.
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert type(llm.build_llm()).__name__ == "ChatOpenAI"


def test_nothing_usable_names_the_preferred_backend_and_the_fix(monkeypatch):
    _configure_gateway(monkeypatch)
    _selective_probe(monkeypatch, rejected={"gateway", "claude", "ollama"})

    info = llm.check_backend()
    assert info["ok"] is False
    assert info["type"] == "gateway"
    assert info["skipped"] == ["gateway", "ollama"]
    assert "AGENT_LLM_GATEWAY_URL" in info["fix"]


def test_ollama_backend_sends_the_configured_context_window(monkeypatch):
    """Ollama's default window is 4096 tokens, and it truncates from the *front*.

    Without an explicit ``num_ctx`` the scoped packs (~5k tokens) lost their system prompt:
    on n1 on 2026-09-10 Ollama logged ``truncating input prompt limit=4096 prompt=5072`` and
    every turn ran into the graph timeout.
    """
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AGENT_OLLAMA_NUM_CTX", 16384)
    assert llm.build_llm().num_ctx == 16384


def test_ollama_context_window_zero_leaves_the_server_default(monkeypatch):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AGENT_OLLAMA_NUM_CTX", 0)
    assert llm.build_llm().num_ctx is None


def test_gateway_client_targets_the_v1_api_and_forwards_the_ollama_hints(monkeypatch):
    """The tuned context window must survive the move to the gateway (ADR 0152 d2)."""
    _configure_gateway(monkeypatch)
    monkeypatch.setattr(config, "AGENT_OLLAMA_NUM_CTX", 16384)
    monkeypatch.setattr(config, "AGENT_OLLAMA_KEEP_ALIVE", "30m")
    monkeypatch.setattr(config, "AGENT_OLLAMA_REASONING", False)

    built = llm.build_llm()
    assert str(built.openai_api_base) == "http://gw:8020/v1"
    assert built.max_retries == 0  # retrying is the gateway's job, bounded by its budget
    assert built.extra_body == {
        "examlops": {"ollama": {"keep_alive": "30m", "num_ctx": 16384, "think": False}}
    }
    assert llm.build_llm(model="qwen3:14b").model_name == "qwen3:14b"


def test_gateway_key_may_come_from_a_file(monkeypatch, tmp_path):
    """A key in a mounted secret file never appears in the container's environment listing."""
    monkeypatch.delenv("AGENT_LLM_GATEWAY_KEY", raising=False)
    secret = tmp_path / "key"
    secret.write_text("vk-from-file\n")
    monkeypatch.setenv("AGENT_LLM_GATEWAY_KEY_FILE", str(secret))
    assert config._secret("AGENT_LLM_GATEWAY_KEY", "AGENT_LLM_GATEWAY_KEY_FILE") == "vk-from-file"
    monkeypatch.setenv("AGENT_LLM_GATEWAY_KEY", "vk-from-env")  # the env value wins
    assert config._secret("AGENT_LLM_GATEWAY_KEY", "AGENT_LLM_GATEWAY_KEY_FILE") == "vk-from-env"
    monkeypatch.setenv("AGENT_LLM_GATEWAY_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("AGENT_LLM_GATEWAY_KEY")
    assert config._secret("AGENT_LLM_GATEWAY_KEY", "AGENT_LLM_GATEWAY_KEY_FILE") == ""


def test_there_is_no_azure_backend_left(monkeypatch):
    """The platform may not start new Azure activity; the code path is gone, not merely unused."""
    assert not hasattr(config, "AZURE_OPENAI_API_KEY")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/openai/v1")
    _clear_backends(monkeypatch)
    assert llm._configured_types() == ["ollama"]


def test_default_context_window_fits_the_largest_specialist_prompt():
    """The default must hold the biggest pack's system prompt + tool schemas, with headroom.

    Tokens are estimated at 3 chars each, deliberately pessimistic (JSON schemas measured
    closer to 4.5), plus 4096 tokens for the conversation and tool results. Adding tools to a
    pack until this fails means the default has to grow with it, not that the test is wrong.
    """
    import json

    from langchain_core.utils.function_calling import convert_to_openai_tool
    from skipper import skills
    from skipper import tools as inrepo
    from skipper.prompts import system_prompt

    packs = skills.toolsets(inrepo.TOOLS, extra_tools=[])
    base = len(system_prompt())
    worst = max(
        base
        + len(spec.playbook)
        + len(json.dumps([convert_to_openai_tool(t) for t in packs.get(spec.name) or []]))
        for spec in skills.ALL
    )
    assert config.AGENT_OLLAMA_NUM_CTX >= worst // 3 + 4096
