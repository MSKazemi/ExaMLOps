# tests/unit/test_serving_backends.py
"""E1 — Kubernetes-native serving (ADR 0015, spec E1).

GWT-2 manifest generation + validation · GWT-3 canary rollout · GWT-5 backend selection ·
GWT-6 verify-before-load refuse · LLM manifest variant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import serving_backends as sb  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

_JPCP = {"name": "JPCP", "task_type": "regression", "framework": "sklearn"}
_LLM = {
    "name": "ChatModel",
    "task_type": "text_generation",
    "engine": {"engine": "vllm", "dtype": "float16", "quantization": "awq", "max_model_len": 4096},
}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-key")
    init_db()


# ── GWT-5: backend selection ──────────────────────────────────────────────────


def test_gwt5_default_backend_is_compose(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SERVING_BACKEND", raising=False)
    assert sb.select_backend().name == "ray-compose"


def test_backend_selectable_via_env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SERVING_BACKEND", "kserve-k8s")
    assert sb.select_backend().name == "kserve-k8s"
    assert isinstance(sb.select_backend(), sb.ServingBackend)


# ── GWT-2: manifest generation + validation ──────────────────────────────────


def test_gwt2_inference_service_manifest():
    m = sb.registry_to_kserve(_JPCP, "Production")
    assert m["kind"] == "InferenceService"
    assert m["metadata"]["name"] == "jpcp"
    assert m["spec"]["predictor"]["model"]["storageUri"] == "mlflow://models/jpcp@Production"
    assert sb.validate_manifest(m) == []


def test_llm_manifest_variant_and_engine_args():
    m = sb.registry_to_kserve(_LLM, "Production")
    assert m["kind"] == "LLMInferenceService"
    args = m["spec"]["predictor"]["model"]["args"]
    assert "--dtype" in args and "float16" in args
    assert "--quantization" in args and "awq" in args
    assert sb.validate_manifest(m) == []


def test_validate_catches_bad_manifest():
    bad = {"apiVersion": "v1", "kind": "InferenceService", "metadata": {"name": "a b"}, "spec": {}}
    errors = sb.validate_manifest(bad)
    assert any("predictor" in e for e in errors)
    assert any("DNS-safe" in e for e in errors)


# ── GWT-3: canary rollout ─────────────────────────────────────────────────────


def test_gwt3_canary_manifest():
    m = sb.registry_to_kserve(_JPCP, "Canary", canary_pct=10)
    assert m["spec"]["predictor"]["canaryTrafficPercent"] == 10
    assert sb.validate_manifest(m) == []


def test_canary_out_of_range_flagged():
    m = sb.registry_to_kserve(_JPCP, "Canary", canary_pct=150)
    assert any("canaryTrafficPercent" in e for e in sb.validate_manifest(m))


# ── GWT-6: verify-before-load ────────────────────────────────────────────────


def test_gwt6_verify_before_load_refuses_tampered(tmp_path):
    from examlops import supplychain

    art = tmp_path / "model.pkl"
    art.write_bytes(b"good-weights")
    supplychain.sign_model("JPCP", "17", [art])
    supplychain._verify_cache.clear()
    art.write_bytes(b"TAMPERED")

    backend = sb.KServeK8s()
    assert backend.verify_before_load("JPCP", "17", [art], mode="enforce") is False


def test_verify_before_load_allows_valid(tmp_path):
    from examlops import supplychain

    art = tmp_path / "model.pkl"
    art.write_bytes(b"good-weights")
    supplychain.sign_model("JPCP", "17", [art])
    supplychain._verify_cache.clear()

    backend = sb.KServeK8s()
    assert backend.verify_before_load("JPCP", "17", [art], mode="enforce") is True


# ── backend deploy (dry-run, no cluster) ─────────────────────────────────────


def test_compose_backend_deploy_shape():
    out = sb.RayServeCompose().deploy("JPCP", "17", "Production")
    assert out["backend"] == "ray-compose"


def test_kserve_deploy_generates_manifest(tmp_path):
    reg = tmp_path / "models"
    reg.mkdir()
    import yaml

    (reg / "jpcp.yaml").write_text(yaml.safe_dump(_JPCP))
    backend = sb.KServeK8s(registry_dir=str(reg))
    out = backend.deploy("JPCP", "17", "Production")
    assert out["manifest"]["kind"] == "InferenceService"
