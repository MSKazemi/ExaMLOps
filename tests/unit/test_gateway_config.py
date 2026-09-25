"""gateway.yaml (ADR 0155): strict validation with paths, discovery, generated default, reload safety."""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway.config import (
    ConfigError,
    ProviderCfg,
    build_runtime,
    default_provider_factory,
    generated_config,
    load_config_file,
    validate_config,
)
from examlops.gateway.providers import OllamaProvider, OpenAICompatProvider


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


async def test_build_runtime_threads_the_warm_flag_onto_the_deployment():
    cfg = json.loads(json.dumps(GOOD))
    cfg["models"]["chat"]["deployments"][0]["warm"] = True
    rt = await build_runtime(cfg, provider_factory=factory({}), source="file")
    assert rt.catalog.routes["chat"].deployments[0].warm is True
    assert rt.catalog.routes["small"].deployments[0].warm is False  # default, unset
    assert [d.key for d in rt.core.warm_deployments()] == ["n1/qwen3:8b"]


async def test_discovery_adds_both_chat_and_embedding_models_but_nothing_else():
    """Anything the gateway can actually route a request to (ADR 0152 d1: chat, and — since
    `/v1/embeddings` shipped — embeddings) is discoverable; a model with neither capability isn't,
    because no route exists that could ever serve it."""
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["n1"]["discover"] = True
    rt = await build_runtime(
        cfg,
        provider_factory=factory(
            {
                "qwen3:8b": ["completion", "tools"],
                "gemma3:12b": ["completion"],
                "nomic-embed-text": ["embedding"],
            }
        ),
    )
    assert "gemma3:12b" in rt.catalog.routes
    assert "nomic-embed-text" in rt.catalog.routes
    embed_dep = rt.catalog.routes["nomic-embed-text"].deployments[0]
    assert embed_dep.capabilities is not None and embed_dep.capabilities.embeddings is True
    dep = rt.catalog.routes["gemma3:12b"].deployments[0]
    assert dep.capabilities is not None and dep.capabilities.tools is False
    # a configured route's deployment also learns what the server says about its model
    chat_dep = rt.catalog.routes["chat"].deployments[0]
    assert chat_dep.capabilities is not None and chat_dep.capabilities.tools is True
    # and an operator-written route is not overwritten by discovery
    assert rt.catalog.routes["chat"].fallbacks == ["small"]


async def test_discovery_excludes_a_model_with_neither_chat_nor_embedding_capability():
    """A *declared* (non-empty) capability set naming neither is excluded. An empty/absent list is
    a different case (older Ollama servers report none at all) and is deliberately permissive —
    see ``_capabilities()``'s own docstring — so this uses an explicit, non-empty declaration."""
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["n1"]["discover"] = True
    rt = await build_runtime(cfg, provider_factory=factory({"vision-only": ["vision"]}))
    assert "vision-only" not in rt.catalog.routes


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


async def test_generated_default_routes_every_discovered_chat_and_embedding_model(monkeypatch):
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
    # A zero-config deployment must be able to serve a real embedding call too (`/v1/embeddings`,
    # ADR 0152 d1) — not just chat, which was the whole catalog before that endpoint existed.
    assert set(rt.catalog.routes) == {"qwen3:8b", "llama3.2:3b", "e"}
    assert rt.catalog.routes["e"].deployments[0].capabilities.embeddings is True
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


# ── openai_compat provider type (ADR 0152 d3, PLAN.md P5) ─────────────────────


def test_openai_compat_is_a_valid_provider_type():
    cfg = json.loads(json.dumps(GOOD))
    cfg["providers"]["router"] = {
        "type": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "locality": "external",
        "external_ok": True,
    }
    assert errs(cfg) == []


def test_default_provider_factory_builds_an_openai_compat_provider():
    cfg = ProviderCfg(
        type="openai_compat", base_url="https://openrouter.ai/api/v1", locality="external"
    )
    provider = default_provider_factory("router", cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.base_url == "https://openrouter.ai/api/v1"


def test_default_provider_factory_still_builds_ollama_by_default():
    """A regression guard for the dispatch itself: adding the new branch must not change what an
    unqualified/`ollama` config builds."""
    cfg = ProviderCfg(type="ollama", base_url="http://127.0.0.1:11434")
    assert isinstance(default_provider_factory("n1", cfg), OllamaProvider)


def test_api_key_env_is_resolved_from_the_environment_not_stored_in_the_file(monkeypatch):
    """The config file only ever names an env var (`api_key_env`); the literal secret lives in the
    process environment, never on disk — see `ProviderCfg.api_key_env`'s own docstring."""
    monkeypatch.setenv("MY_ROUTER_KEY", "sk-live-abc")
    cfg = ProviderCfg(
        type="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        locality="external",
        api_key_env="MY_ROUTER_KEY",
    )
    provider = default_provider_factory("router", cfg)
    assert provider._api_key == "sk-live-abc"  # noqa: SLF001 - the only way to observe it landed


def test_api_key_env_unset_in_this_process_builds_unauthenticated_not_a_crash(monkeypatch):
    monkeypatch.delenv("MY_ROUTER_KEY", raising=False)
    cfg = ProviderCfg(
        type="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        locality="external",
        api_key_env="MY_ROUTER_KEY",
    )
    provider = default_provider_factory("router", cfg)  # must not raise
    assert provider._api_key is None  # noqa: SLF001


def test_openai_compat_quirks_flow_from_config_to_the_provider():
    cfg = ProviderCfg(
        type="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        locality="external",
        send_stream_options=False,
        retry_after_is_ms=True,
        constrains_schema=True,
    )
    provider = default_provider_factory("router", cfg)
    assert provider.quirks.send_stream_options is False
    assert provider.quirks.retry_after_is_ms is True
    assert provider.constrains_schema is True


async def test_build_runtime_routes_to_an_openai_compat_provider():
    def openai_models_transport() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(
                    200,
                    json={
                        "model": "router-picked",
                        "choices": [
                            {"index": 0, "message": {"content": "hi"}, "finish_reason": "stop"}
                        ],
                    },
                )
            return httpx.Response(404, json={"error": "not found"})

        return httpx.MockTransport(handler)

    def make(name, pcfg):
        return OpenAICompatProvider(
            name, pcfg.base_url, locality=pcfg.locality, transport=openai_models_transport()
        )

    cfg = {
        "version": 1,
        "defaults": {"allowed_localities": ["local", "site", "external"]},
        "providers": {
            # A reserved, non-resolving TLD (RFC 2606) — same convention as `ollama.test` in the
            # Ollama provider's own tests. A *real*, resolvable host here would make this test
            # perform a live DNS lookup (BL-111's per-connection re-check), which is both a live
            # network dependency a unit test must not have and, as found while writing this test,
            # can trip on a real address the resolver returns for reasons unrelated to this code.
            "router": {
                "type": "openai_compat",
                "base_url": "https://router.test/v1",
                "locality": "external",
                "external_ok": True,
            }
        },
        "models": {"auto": {"deployments": [{"provider": "router", "model": "auto"}]}},
        "aliases": {},
    }
    rt = await build_runtime(cfg, provider_factory=make)
    dep = rt.catalog.routes["auto"].deployments[0]
    result = await dep.provider.chat(dep_request(dep.model))
    assert result.text == "hi" and result.model == "router-picked"


def dep_request(model: str):
    from examlops.gateway.providers.base import ChatRequest

    return ChatRequest(model=model, messages=[{"role": "user", "content": "hi"}])
