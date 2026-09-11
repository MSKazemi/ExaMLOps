# tests/unit/test_kserve_render_schema.py
"""USAR I0 — KServe renders are checked against the real API, offline (ADR 0142 d2–d4).

The renderer used to emit an ``LLMInferenceService`` with a ``spec.predictor`` block the CRD does not
have, an ``mlflow://`` storage URI no KServe storage initializer can read, and a predictive
``InferenceService`` with no ``modelFormat``. Its hand-written validator checked for exactly the
field that was wrong, so every test passed on manifests the API server would reject.

These tests validate every render against the ``openAPIV3Schema`` vendored from the pinned KServe
release (spec-usar-1 R-SUB-21), with unknown fields rejected the way ``kubectl --validate=strict``
rejects them — and they prove the check can fail by injecting the historical defect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.engines.config import EngineConfig, to_vllm_args  # noqa: E402
from examlops.serving.substrates import k8s_schema, kserve  # noqa: E402
from examlops.serving.substrates.resolve import RenderError, ResolvedRef  # noqa: E402

_REF = ResolvedRef(
    model="jpcp",
    version="17",
    alias="Production",
    artifact_uri="s3://mlflow-artifacts/1/models/m-abc/artifacts",
    digest="sha256:" + "a" * 64,
    project="research",
)
_SKLEARN = {"name": "JPCP", "task_type": "regression", "framework": "sklearn"}
_LLM = {
    "name": "ChatModel",
    "task_type": "text_generation",
    "engine": {"engine": "vllm", "dtype": "float16", "quantization": "awq", "max_model_len": 4096},
}
_LLM_REF = ResolvedRef(
    model="chatmodel",
    version="3",
    alias="Production",
    artifact_uri="s3://mlflow-artifacts/2/models/m-llm/artifacts",
    digest="unsigned",
    project="default",
)


# ── the pin ──────────────────────────────────────────────────────────────────


def test_exactly_one_kserve_version_is_pinned_and_its_schemas_load():
    pin = k8s_schema.current_pin()
    assert pin.version == "v0.20.0"
    assert set(k8s_schema.pinned_kinds()) == {
        ("InferenceService", "serving.kserve.io/v1beta1"),
        ("LLMInferenceService", "serving.kserve.io/v1alpha2"),
    }
    for kind, api in k8s_schema.pinned_kinds():
        schema = k8s_schema.load_schema(kind)
        assert schema["type"] == "object" and "spec" in schema["properties"], (kind, api)


# ── the validator ────────────────────────────────────────────────────────────


def test_a_minimal_valid_inference_service_passes():
    obj = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": "jpcp"},
        "spec": {
            "predictor": {"model": {"modelFormat": {"name": "sklearn"}, "storageUri": "s3://b/k"}}
        },
    }
    assert k8s_schema.validate(obj) == []


def test_unknown_fields_are_rejected_like_strict_kubectl():
    obj = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": "jpcp"},
        "spec": {"predictor": {"model": {"modelFormat": {"name": "sklearn"}}, "bogus": 1}},
    }
    errors = k8s_schema.validate(obj)
    assert any("spec.predictor.bogus" in e and "unknown field" in e for e in errors), errors


def test_int_or_string_quantities_accept_both_forms():
    for cpu in ("500m", 1):
        obj = {
            "apiVersion": "serving.kserve.io/v1beta1",
            "kind": "InferenceService",
            "metadata": {"name": "jpcp"},
            "spec": {
                "predictor": {
                    "model": {
                        "modelFormat": {"name": "sklearn"},
                        "resources": {"limits": {"cpu": cpu}},
                    }
                }
            },
        }
        assert k8s_schema.validate(obj) == [], cpu


def test_wrong_types_and_names_are_reported():
    obj = {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": "JPCP model"},
        "spec": {"predictor": {"minReplicas": "two"}},
    }
    errors = k8s_schema.validate(obj)
    assert any("metadata.name" in e for e in errors), errors
    assert any("spec.predictor.minReplicas" in e for e in errors), errors


def test_an_unpinned_kind_or_api_version_is_refused():
    obj = {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "LLMInferenceService",
        "metadata": {"name": "x"},
        "spec": {},
    }
    errors = k8s_schema.validate(obj)
    assert any("not pinned" in e for e in errors), errors


# ── predictive render (R-SUB-8/9/10/18/19) ───────────────────────────────────


def test_sklearn_renders_a_schema_valid_standard_mode_inference_service():
    m = kserve.render_inference_service(_SKLEARN, _REF)
    assert m["apiVersion"] == "serving.kserve.io/v1beta1" and m["kind"] == "InferenceService"
    assert m["metadata"]["annotations"]["serving.kserve.io/deploymentMode"] == "Standard"
    model = m["spec"]["predictor"]["model"]
    assert model["modelFormat"] == {"name": "sklearn"}
    assert model["runtime"] == "kserve-sklearnserver"
    assert model["protocolVersion"] == "v2"
    assert model["storageUri"] == _REF.artifact_uri
    assert k8s_schema.validate(m) == []


def test_labels_and_digest_name_exactly_what_runs():
    m = kserve.render_inference_service(_SKLEARN, _REF)
    labels = m["metadata"]["labels"]
    assert labels["examlops.io/model"] == "jpcp"
    assert labels["examlops.io/version"] == "17"
    assert labels["examlops.io/alias"] == "Production"
    assert labels["examlops.io/project"] == "research"
    assert labels["app.kubernetes.io/managed-by"] == "examlops"
    assert m["metadata"]["annotations"]["examlops.io/artifact-digest"] == _REF.digest


def test_the_canary_uses_the_standard_mode_field_not_the_knative_one():
    canary = ResolvedRef(
        "jpcp",
        "18",
        "Canary",
        "s3://mlflow-artifacts/1/models/m-def/artifacts",
        "unsigned",
        "research",
    )
    m = kserve.render_inference_service(_SKLEARN, _REF, canary=canary, canary_pct=10)
    assert "canaryTrafficPercent" not in m["spec"]["predictor"]
    (entry,) = m["spec"]["canary"]
    assert entry["trafficPercent"] == 10
    assert entry["predictor"]["model"]["storageUri"] == canary.artifact_uri
    # v0.20.0: predictor.name names each variant's Deployment; both are named by version so a
    # promotion can rename the stable to the canary's name without a restart.
    assert m["spec"]["predictor"]["name"] == "v17" and entry["predictor"]["name"] == "v18"
    assert m["metadata"]["annotations"]["examlops.io/canary-version"] == "18"
    assert k8s_schema.validate(m) == []


@pytest.mark.parametrize("pct", [-1, 101])
def test_canary_percent_outside_0_100_is_refused(pct):
    canary = ResolvedRef("jpcp", "18", "Canary", "s3://b/k", "unsigned", "research")
    with pytest.raises(RenderError):
        kserve.render_inference_service(_SKLEARN, _REF, canary=canary, canary_pct=pct)


def test_a_canary_of_the_same_version_is_refused():
    with pytest.raises(RenderError):
        kserve.render_inference_service(_SKLEARN, _REF, canary=_REF, canary_pct=10)


# ── generative render (R-SUB-12/13/14/15) ────────────────────────────────────


def test_llm_renders_a_schema_valid_v1alpha2_service_with_no_predictor():
    m = kserve.render_llm_inference_service(_LLM, _LLM_REF)
    assert m["apiVersion"] == "serving.kserve.io/v1alpha2"
    assert m["kind"] == "LLMInferenceService"
    assert "predictor" not in m["spec"]
    assert m["spec"]["model"] == {"uri": _LLM_REF.artifact_uri, "name": "chatmodel"}
    assert m["spec"]["router"] == {"scheduler": {}, "route": {}, "gateway": {}}
    assert "prefill" not in m["spec"]
    assert k8s_schema.validate(m) == []


def test_llm_args_are_exactly_the_shared_renderer_output():
    m = kserve.render_llm_inference_service(_LLM, _LLM_REF)
    (main,) = m["spec"]["template"]["containers"]
    assert main["name"] == "main"
    assert main["args"] == to_vllm_args(EngineConfig.from_dict(_LLM["engine"]))


def test_parallelism_renders_into_the_parallelism_block():
    llm = {
        **_LLM,
        "engine": {**_LLM["engine"], "tensor_parallel_size": 4, "pipeline_parallel_size": 2},
    }
    m = kserve.render_llm_inference_service(llm, _LLM_REF)
    assert m["spec"]["parallelism"] == {"tensor": 4, "pipeline": 2}
    assert k8s_schema.validate(m) == []


def test_served_model_name_is_the_openai_model_string():
    llm = {**_LLM, "engine": {**_LLM["engine"], "served_model_name": "chat-7b"}}
    m = kserve.render_llm_inference_service(llm, _LLM_REF)
    assert m["spec"]["model"]["name"] == "chat-7b"


def test_the_historical_llm_shape_fails_schema_validation():
    """The guard can fail: the old `spec.predictor` shape is rejected by the pinned schema."""
    m = kserve.render_llm_inference_service(_LLM, _LLM_REF)
    m["spec"]["predictor"] = {"model": {"storageUri": "mlflow://models/chatmodel@Production"}}
    errors = k8s_schema.validate(m)
    assert any("spec.predictor" in e and "unknown field" in e for e in errors), errors


def test_render_dispatch_picks_the_kind_from_the_model_yaml():
    assert kserve.render(_SKLEARN, _REF)["kind"] == "InferenceService"
    assert kserve.render(_LLM, _LLM_REF)["kind"] == "LLMInferenceService"
