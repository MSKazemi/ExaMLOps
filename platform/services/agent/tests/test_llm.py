from exa_agent import config, llm


def _clear_backends(monkeypatch):
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")


def test_azure_backend_preferred_when_configured(monkeypatch):
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


def test_claude_backend_when_no_azure(monkeypatch):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    info = llm.check_backend()
    assert info["type"] == "claude"


def test_azure_needs_both_key_and_endpoint(monkeypatch):
    _clear_backends(monkeypatch)
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")  # endpoint missing
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test")

    # With endpoint unset, Azure must NOT win — falls through to Claude.
    assert llm.check_backend()["type"] == "claude"
