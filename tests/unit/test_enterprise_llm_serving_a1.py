# tests/unit/test_enterprise_llm_serving_a1.py
"""Enterprise LLM Serving — Track A / A1 engine binding (ADR 0096, spec §4.1).

GWT-A1 gateway→engine dispatch · GWT-A2 sampling passthrough + incremental stream ·
GWT-A3 engine health reachable through the gateway · GWT-A8 echo fallback with no vLLM.

Every assertion runs on CPU with no GPU/vLLM: the gateway→engine path degrades to
``EchoEngine`` so the keystone wiring is fully exercisable in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops import gateway as gw  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


# ── GWT-A1: gateway resolves a backend to a local engine ──────────────────────


def test_gwta1_gateway_dispatches_through_build_engine():
    router = gw.build_engine_router("my-llm", engines.EngineConfig(engine="echo"))
    client = gw.GatewayClient(router)
    comp = client.chat("my-llm", [{"role": "user", "content": "hello world"}])
    assert comp.text  # a completion came back
    assert comp.backend == "echo"  # served by the local engine, not a remote route


def test_gwta1_engine_backend_carries_engine_and_health():
    backend = gw.engine_backend("my-llm", engines.EngineConfig(engine="echo"))
    assert backend.engine.name == "echo"
    assert backend.health() is True


def test_gwta1_engine_backend_accepts_dict_config():
    # A per-model YAML `engine:` block (dict) is accepted, not only EngineConfig.
    backend = gw.engine_backend("my-llm", {"engine": "echo"})
    assert backend.engine.name == "echo"


# ── GWT-A2: sampling params flow through to the engine ────────────────────────


def test_gwta2_sampling_kwargs_helper_maps_and_filters():
    got = engines._sampling_kwargs(
        {"temperature": 0, "max_tokens": 8, "top_p": 0.9, "unknown": 1, "seed": None}
    )
    assert got == {
        "temperature": 0,
        "max_tokens": 8,
        "top_p": 0.9,
    }  # temp=0 kept, None/unknown dropped


def test_gwta2_max_tokens_forwarded_through_gateway():
    router = gw.build_engine_router("my-llm", engines.EngineConfig(engine="echo"))
    client = gw.GatewayClient(router)
    comp = client.chat("my-llm", [{"role": "user", "content": "a b c d e f"}], max_tokens=2)
    # EchoEngine honours max_tokens → proves sampling params reach the engine.
    assert comp.completion_tokens == 2


def test_gwta2_stream_yields_incremental_chunks():
    eng = engines.EchoEngine()
    chunks = list(eng.stream("one two three", max_tokens=3))
    assert len(chunks) >= 2  # real incremental deltas, not one whole-completion chunk


# ── GWT-A3: engine readiness is real and reachable through the gateway ────────


def test_gwta3_vllm_engine_not_ready_before_load():
    eng = engines.VLLMEngine("some/model")
    assert eng.health() is False  # not loaded ⇒ not ready (no GPU needed for this)


def test_gwta3_health_surface_reachable_through_gateway():
    router = gw.build_engine_router("my-llm", engines.EngineConfig(engine="echo"))
    client = gw.GatewayClient(router)
    health = client.health()
    assert health == {"my-llm:engine:my-llm": True}


# ── GWT-A8: no vLLM installed ⇒ degrade to echo, path still serves ────────────


def test_gwta8_build_engine_falls_back_to_echo_without_vllm(monkeypatch):
    # Force the dep-probe to report vLLM absent (as on a CPU/CI host).
    monkeypatch.setattr(engines, "_dep_available", lambda name: False)
    with pytest.warns(RuntimeWarning, match="falling back to EchoEngine"):
        eng = engines.build_engine(engines.EngineConfig(engine="vllm"), model_path="m")
    assert eng.name == "echo"


def test_gwta8_build_engine_strict_no_fallback(monkeypatch):
    monkeypatch.setattr(engines, "_dep_available", lambda name: False)
    # allow_fallback=False never silently downgrades. Since ADR 0107 `vllm` means
    # *server* mode, so with no endpoint configured there is no real engine to hand
    # back — it raises with an actionable message rather than returning something that
    # would fail confusingly on first use.
    with pytest.raises(RuntimeError, match="no endpoint is configured"):
        engines.build_engine(
            engines.EngineConfig(engine="vllm"), model_path="m", allow_fallback=False
        )


def test_gwta8_strict_inproc_keeps_real_engine(monkeypatch):
    # The in-process engine is still requestable explicitly, and strict mode hands back
    # the real (dep-less, therefore unusable) engine rather than an echo stand-in.
    monkeypatch.setattr(engines, "_dep_available", lambda name: False)
    eng = engines.build_engine(
        engines.EngineConfig(engine="vllm-inproc"), model_path="m", allow_fallback=False
    )
    assert eng.name == "vllm-inproc"


def test_gwta8_gateway_serves_via_echo_when_vllm_absent(monkeypatch):
    monkeypatch.setattr(engines, "_dep_available", lambda name: False)
    with pytest.warns(RuntimeWarning):
        router = gw.build_engine_router("my-llm", engines.EngineConfig(engine="vllm"))
    client = gw.GatewayClient(router)
    comp = client.chat("my-llm", [{"role": "user", "content": "hi there"}])
    assert comp.backend == "echo"  # R-A8: full gateway→engine path on the echo backend


def test_gwta8_spec_decode_config_preserved_on_fallback(monkeypatch):
    monkeypatch.setattr(engines, "_dep_available", lambda name: False)
    cfg = engines.EngineConfig(
        engine="vllm", speculative_decoding={"enabled": True, "draft_model": "tiny"}
    )
    with pytest.warns(RuntimeWarning):
        eng = engines.build_engine(cfg, model_path="m")
    # spec-decode telemetry stays exercisable through the fallback engine.
    comp = eng.generate("one two three", max_tokens=3)
    assert comp.proposed_tokens == 3
    assert comp.accepted_tokens == 3


# ── Dependency direction: engines MUST NOT import gateway (spec R-A1) ──────────


def test_engines_does_not_import_gateway():
    src = (
        Path(__file__).parents[2]
        / "platform"
        / "cli"
        / "src"
        / "examlops"
        / "engines"
        / "__init__.py"
    ).read_text()
    assert "import gateway" not in src
    assert "from examlops.gateway" not in src
