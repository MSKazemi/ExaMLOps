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
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture
def probe(monkeypatch):
    """Stub only the httpx name *inside skipper.llm*, not the httpx module.

    These tests must never touch the network: the previous version asserted a
    healthy Azure backend using a fake key against the *real* Foundry endpoint,
    and passed only because a 401 counted as healthy.

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


def test_azure_backend_preferred_when_configured(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")  # should be overridden
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setattr(
        config, "AZURE_OPENAI_ENDPOINT", "https://examlops.services.ai.azure.com/openai/v1/"
    )
    monkeypatch.setattr(config, "AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini")

    info = llm.check_backend()
    assert info == {"ok": True, "type": "azure", "model": "gpt-5.4-mini"}

    built = llm.build_llm()
    # langchain-openai's ChatOpenAI exposes the model id as `model_name`.
    assert built.model_name == "gpt-5.4-mini"


def test_claude_backend_when_no_azure(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    info = llm.check_backend()
    assert info["type"] == "claude"


def test_azure_needs_both_key_and_endpoint(monkeypatch, probe):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")  # endpoint missing
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    # With endpoint unset, Azure must NOT win — falls through to Claude.
    assert llm.check_backend()["type"] == "claude"


def _configure_azure(monkeypatch):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setattr(
        config, "AZURE_OPENAI_ENDPOINT", "https://examlops.services.ai.azure.com/openai/v1/"
    )
    monkeypatch.setattr(config, "AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credential_reports_unhealthy(monkeypatch, probe, status):
    """A live endpoint that refuses the key must NOT report ok:True.

    Regression test for the real LXP outage of 2026-08-20: the Foundry endpoint
    was up and answering 401 to a stored key, and check_backend() reported the
    Azure backend healthy — so the agent looked fine while being unable to
    answer a single request.
    """
    _configure_azure(monkeypatch)
    probe.status["code"] = status

    info = llm.check_backend()
    assert info["ok"] is False
    # The *preferred* backend is the one named: it is the one whose config the operator meant
    # to use, so it is the one worth pointing at.
    assert info["type"] == "azure"
    assert info["model"] == "gpt-5.4-mini"
    # …and the report says what was tried and which variable to repair, because "not ok" alone
    # is what sent an operator hunting in the wrong place.
    assert info["skipped"] == ["azure", "ollama"]
    assert "AZURE_OPENAI_API_KEY" in info["fix"]


def test_azure_probe_sends_the_key(monkeypatch, probe):
    """The probe must authenticate, else it cannot tell a good key from a bad one."""
    _configure_azure(monkeypatch)

    llm.check_backend()

    url, headers = probe.calls[-1]
    assert url.endswith("/models"), url
    assert headers.get("Authorization") == "Bearer azure-key"


def test_non_auth_status_is_still_reachable(monkeypatch, probe):
    """404/5xx are not credential failures — probe paths differ per provider."""
    _configure_azure(monkeypatch)
    probe.status["code"] = 404

    assert llm.check_backend()["ok"] is True


def test_transport_error_reports_unhealthy(monkeypatch, probe):
    _configure_azure(monkeypatch)
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
            hit = "azure" if "azure" in url else "claude" if "anthropic" in url else "ollama"
            return _Resp(401 if hit in rejected else 200)

    monkeypatch.setattr(
        llm,
        "httpx",
        _t.SimpleNamespace(Client=lambda *a, **k: _Client(), RequestError=httpx.RequestError),
    )


def test_rejected_preferred_backend_falls_back_to_a_working_one(monkeypatch):
    """One stale key must not take the agent down while a working backend sits behind it.

    This is the 2026-08-20 outage: Azure was configured, its key was rejected, and the agent
    built an Azure client anyway that raised AuthenticationError on the first token.
    """
    _configure_azure(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"azure"})

    info = llm.check_backend()
    assert info["ok"] is True
    assert info["type"] == "claude"
    # The fallback is reported, never silent: it changes the quality of every answer.
    assert info["skipped"] == ["azure"]


def test_build_llm_uses_the_backend_that_was_found_working(monkeypatch):
    """The reported backend and the used backend must be the same one."""
    _configure_azure(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"azure"})

    llm.check_backend()
    built = llm.build_llm()
    assert type(built).__name__ == "ChatAnthropic"


def test_build_llm_does_no_io_and_keeps_the_preferred_backend_when_unprobed(monkeypatch):
    """Building must not reach the network — probing belongs at the entry points.

    ``build_graph()`` calls ``build_llm()``; if that probed, every graph construction would
    make HTTP calls.
    """
    _configure_azure(monkeypatch)

    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("build_llm performed I/O")

    monkeypatch.setattr(llm, "_endpoint_reachable", _explode)
    assert type(llm.build_llm()).__name__ == "ChatOpenAI"


def test_a_resolved_backend_that_is_no_longer_configured_is_ignored(monkeypatch):
    """Stale module state must never outrank the current config."""
    _configure_azure(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")
    _selective_probe(monkeypatch, rejected={"azure"})
    llm.check_backend()
    assert llm._RESOLVED_TYPE == "claude"

    # The Claude key goes away; the next build must not try to use it.
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert type(llm.build_llm()).__name__ == "ChatOpenAI"


def test_nothing_usable_names_the_preferred_backend_and_the_fix(monkeypatch):
    _configure_azure(monkeypatch)
    _selective_probe(monkeypatch, rejected={"azure", "claude", "ollama"})

    info = llm.check_backend()
    assert info["ok"] is False
    assert info["type"] == "azure"
    assert info["skipped"] == ["azure", "ollama"]
    assert "AZURE_OPENAI_ENDPOINT" in info["fix"]
