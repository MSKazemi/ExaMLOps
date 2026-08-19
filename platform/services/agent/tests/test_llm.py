import types

import httpx
import pytest
from skipper import config, llm


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

    assert llm.check_backend() == {"ok": False, "type": "azure", "model": "gpt-5.4-mini"}


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
