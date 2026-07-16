# tests/unit/test_engines.py
"""E2 — Optimized inference engines (ADR 0016, spec E2).

GWT-1 engine contract (echo fallback) · GWT-2 engine-block validation ·
GWT-3 quantize→sign+BOM · GWT-5 spec-decode telemetry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-key")
    init_db()


# ── GWT-1: engine contract ────────────────────────────────────────────────────


def test_gwt1_echo_engine_contract():
    eng = engines.EchoEngine()
    assert isinstance(eng, engines.InferenceEngine)  # satisfies the Protocol
    comp = eng.generate("hello world foo bar", max_tokens=3)
    assert comp.text == "hello world foo"
    assert comp.prompt_tokens == 4
    assert comp.completion_tokens == 3
    assert eng.health() is True


def test_echo_engine_stream():
    eng = engines.EchoEngine()
    chunks = list(eng.stream("a b c", max_tokens=2))
    assert chunks == ["a ", "b "]


def test_build_engine_falls_back_to_echo():
    cfg = engines.EngineConfig(engine="echo")
    eng = engines.build_engine(cfg)
    assert eng.name == "echo"


# ── GWT-2: engine-block validation ────────────────────────────────────────────


def test_gwt2_valid_engine_block():
    block = {
        "engine": "vllm",
        "dtype": "float16",
        "quantization": "awq",
        "max_model_len": 4096,
        "tensor_parallel_size": 2,
        "prefix_cache": True,
    }
    assert engines.validate_engine_block(block) == []


def test_gwt2_invalid_engine_name_fails():
    errs = engines.validate_engine_block({"engine": "notreal"})
    assert any("engine" in e for e in errs)


def test_gwt2_invalid_dtype_and_tp_fail():
    errs = engines.validate_engine_block({"dtype": "float9", "tensor_parallel_size": 0})
    assert len(errs) >= 2


def test_gwt2_specdecode_requires_draft_model():
    errs = engines.validate_engine_block({"speculative_decoding": {"enabled": True}})
    assert any("draft_model" in e for e in errs)
    assert (
        engines.validate_engine_block(
            {"speculative_decoding": {"enabled": True, "draft_model": "tiny"}}
        )
        == []
    )


def test_engine_config_from_dict_roundtrip():
    cfg = engines.EngineConfig.from_dict({"engine": "SGLang", "tensor_parallel_size": 4})
    assert cfg.engine == "sglang"
    assert cfg.tensor_parallel_size == 4


# ── GWT-3: quantize → sign + BOM ──────────────────────────────────────────────


def test_gwt3_quantize_registers_signed_bom_version(tmp_path):
    from examlops.platform_db import get_model_bom, get_model_signature

    art = tmp_path / "model.pkl"
    art.write_bytes(b"weights")
    new_version = engines.quantize_model(
        "JPCP", "17", "awq", artifact_paths=[art], dataset="FData", actor="alice"
    )
    assert new_version == "17-awq"
    # D3: the new version is signed and has a BOM
    assert get_model_signature("JPCP", "17-awq") is not None
    bom = get_model_bom("JPCP", "17-awq")
    assert bom is not None
    assert "quantized:awq" in str(bom)


def test_quantize_rejects_unknown_method():
    with pytest.raises(ValueError):
        engines.quantize_model("JPCP", "17", "notamethod")


def test_quantize_without_artifacts_still_boms(tmp_path):
    from examlops.platform_db import get_model_bom

    v = engines.quantize_model("JPCP", "18", "fp8")
    assert v == "18-fp8"
    assert get_model_bom("JPCP", "18-fp8") is not None


# ── GWT-5: spec-decode telemetry ──────────────────────────────────────────────


def test_gwt5_specdecode_telemetry(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")  # no exporter needed
    cfg = engines.EngineConfig(
        engine="echo", speculative_decoding={"enabled": True, "draft_model": "tiny"}
    )
    eng = engines.EchoEngine(cfg)
    comp = eng.generate("one two three four", max_tokens=4)
    assert comp.proposed_tokens == 4
    assert comp.accepted_tokens == 4
    stats = engines.record_spec_decode_telemetry("JPCP", comp)
    assert stats["acceptance_rate"] == 1.0
    assert stats["speedup"] == 2.0


def test_specdecode_telemetry_zero_when_disabled():
    comp = engines.Completion(text="x", proposed_tokens=0, accepted_tokens=0)
    stats = engines.record_spec_decode_telemetry("JPCP", comp)
    assert stats["acceptance_rate"] == 0.0
