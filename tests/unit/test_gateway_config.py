"""gateway.yaml (ADR 0155): strict validation with paths, discovery, generated default, reload safety."""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway.config import (
    ConfigError,
    build_runtime,
    generated_config,
    load_config_file,
    validate_config,
)
from examlops.gateway.providers import OllamaProvider


def tags_transport(models: dict[str, list[str]]) -> httpx.MockTransport:
    """A fake Ollama exposing ``{model: capabilities}`` via /api/tags (+ empty /api/ps)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": n, "capabilities": c} for n, c in models.items()]}
            )
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def factory(models):
    def make(name, pcfg):
        return OllamaProvider(
            name,
            pcfg.base_url,
            locality=pcfg.locality,
            keep_alive=pcfg.keep_alive,
            options=pcfg.options,
            transport=tags_transport(models),
        )

    return make


GOOD = {
    "version": 1,
    "providers": {
        "n1": {"type": "ollama", "base_url": "http://127.0.0.1:11434", "locality": "local"}
    },
    "models": {
        "chat": {
            "strategy": "priority",
            "deployments": [{"provider": "n1", "model": "qwen3:8b"}],
            "fallbacks": ["small"],
            "required": True,
        },
        "small": {"deployments": [{"provider": "n1", "model": "llama3.2:3b"}]},
    },
    "aliases": {"default": "chat"},
}


def errs(raw) -> list[str]:
    return validate_config(raw)


# ── validation ────────────────────────────────────────────────────────────────


def test_a_good_config_has_no_errors():
    assert errs(GOOD) == []


def test_errors_name_the_path_of_every_problem_at_once():
    bad = json.loads(json.dumps(GOOD))
    bad["models"]["chat"]["deployments"][0]["provider"] = "nope"
    bad["models"]["chat"]["fallbacks"] = ["ghost"]
    bad["providers"]["n1"]["max_concurrency"] = 0
    bad["providers"]["n1"]["colour"] = "red"
    found = errs(bad)
    joined = "\n".join(found)
    assert "models.chat.deployments[0].provider" in joined and "nope" in joined
    assert "models.chat.fallbacks[0]" in joined and "ghost" in joined
    assert "providers.n1.max_concurrency" in joined
    assert "providers.n1.colour" in joined  # unknown keys are errors, not silently ignored
    assert len(found) >= 4  # all reported together, not one per attempt


def test_a_fallback_cycle_is_an_error():
    bad = json.loads(json.dumps(GOOD))
    bad["models"]["small"]["fallbacks"] = ["chat"]
    assert any("cycle" in e and "models.small" in e for e in errs(bad))


def test_an_unknown_provider_type_is_an_error():
    bad = json.loads(json.dumps(GOOD))
    bad["providers"]["n1"]["type"] = "azure_openai"
    assert any("providers.n1.type" in e for e in errs(bad))


@pytest.mark.parametrize(
    "url",
    ["https://acme.openai.azure.com/v1", "http://169.254.169.254/", "http://u:p@h:1/"],
)
def test_forbidden_provider_addresses_are_config_errors(url):
    bad = json.loads(json.dumps(GOOD))
    bad["providers"]["n1"]["base_url"] = url
    assert any("providers.n1.base_url" in e for e in errs(bad))


def test_a_route_with_no_permitted_deployment_is_an_error():
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["omni"] = {
        "type": "ollama",
        "base_url": "https://router.example.org",
        "locality": "external",
        "external_ok": True,
    }
    cfg["models"]["auto"] = {"deployments": [{"provider": "omni", "model": "auto"}]}
    found = errs(cfg)  # default allowed localities exclude `external`
    assert any("models.auto" in e and "locality" in e for e in found)


def test_an_alias_may_not_shadow_a_model_or_dangle():
    bad = json.loads(json.dumps(GOOD))
    bad["aliases"] = {"chat": "small", "x": "ghost"}
    found = "\n".join(errs(bad))
    assert "aliases.chat" in found and "aliases.x" in found


def test_yaml_files_load_and_report_syntax_errors(tmp_path):
    p = tmp_path / "gateway.yaml"
    p.write_text(
        "version: 1\nproviders: {n1: {type: ollama, base_url: 'http://127.0.0.1:11434'}}\nmodels: {}\n"
    )
    assert load_config_file(p)["version"] == 1
    p.write_text("providers: [unclosed")
    with pytest.raises(ConfigError) as ei:
        load_config_file(p)
    assert "gateway.yaml" in str(ei.value)
    with pytest.raises(ConfigError):
        load_config_file(tmp_path / "missing.yaml")


# ── building a runtime ────────────────────────────────────────────────────────


async def test_build_runtime_wires_routes_aliases_and_fallbacks():
    rt = await build_runtime(GOOD, provider_factory=factory({}), source="file")
    assert set(rt.catalog.routes) == {"chat", "small"}
    assert rt.catalog.resolve("default").name == "chat"
    assert rt.catalog.routes["chat"].fallbacks == ["small"] and rt.catalog.routes["chat"].required
    assert rt.source == "file" and rt.allowed_localities == ("local", "site")


async def test_discovery_adds_chat_models_with_capabilities_and_skips_embedding_only():
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["n1"]["discover"] = True
    rt = await build_runtime(
        cfg,
        provider_factory=factory(
            {
                "qwen3:8b": ["completion", "tools"],
                "gemma3:12b": ["completion"],
                "nomic": ["embedding"],
            }
        ),
    )
    assert "gemma3:12b" in rt.catalog.routes and "nomic" not in rt.catalog.routes
    dep = rt.catalog.routes["gemma3:12b"].deployments[0]
    assert dep.capabilities is not None and dep.capabilities.tools is False
    # a configured route's deployment also learns what the server says about its model
    chat_dep = rt.catalog.routes["chat"].deployments[0]
    assert chat_dep.capabilities is not None and chat_dep.capabilities.tools is True
    # and an operator-written route is not overwritten by discovery
    assert rt.catalog.routes["chat"].fallbacks == ["small"]


async def test_discovery_failure_is_a_warning_not_a_crash():
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["n1"]["discover"] = True

    def refuse(request):
        raise httpx.ConnectError("refused")

    def make(name, pcfg):
        return OllamaProvider(name, pcfg.base_url, transport=httpx.MockTransport(refuse))

    rt = await build_runtime(cfg, provider_factory=make)
    assert set(rt.catalog.routes) == {"chat", "small"}
    assert any("n1" in w and "discovery" in w for w in rt.warnings)


async def test_a_dangling_alias_to_an_undiscovered_model_fails_the_build():
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["n1"]["discover"] = True
    cfg["aliases"] = {"default": "does-not-exist"}
    with pytest.raises(ConfigError) as ei:
        await build_runtime(cfg, provider_factory=factory({"qwen3:8b": ["completion"]}))
    assert "aliases.default" in str(ei.value)


async def test_an_invalid_config_never_builds():
    with pytest.raises(ConfigError):
        await build_runtime({"version": 1, "providers": {}, "models": {"m": {"deployments": []}}})


# ── generated default (zero config, ADR 0155 d4) ──────────────────────────────


async def test_generated_default_routes_every_discovered_chat_model(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "qwen3:8b")
    raw = generated_config("http://127.0.0.1:11434")
    assert validate_config(raw) == []
    rt = await build_runtime(
        raw,
        provider_factory=factory(
            {"qwen3:8b": ["completion"], "llama3.2:3b": ["completion"], "e": ["embedding"]}
        ),
        source="generated",
    )
    assert set(rt.catalog.routes) == {"qwen3:8b", "llama3.2:3b"}
    assert rt.catalog.resolve("default").name == "qwen3:8b"  # the agent's own configured model
    assert rt.source == "generated"


async def test_generated_default_drops_a_default_alias_that_was_not_discovered(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "not-installed:1b")
    rt = await build_runtime(
        generated_config("http://127.0.0.1:11434"),
        provider_factory=factory({"llama3.2:3b": ["completion"]}),
        source="generated",
    )
    assert rt.catalog.resolve("default") is None
    assert any("default" in w and "not-installed" in w for w in rt.warnings)
