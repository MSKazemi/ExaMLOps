# tests/unit/test_framework_runtime_map.py
"""USAR I0 — every predictive render says which runtime serves it (spec-usar-1 R-SUB-9/10).

``modelFormat`` is ``+required`` in KServe's Go types, and runtime auto-selection matches on it; the
old renderer emitted neither. The table is verified against KServe v0.20.0 ``config/runtimes``, and
two rules are pinned here: ``pytorch`` never falls through to TorchServe (archived upstream, still
auto-selected), and ``protocolVersion: v2`` is claimed only by runtimes that speak it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.serving.substrates import k8s_schema, kserve  # noqa: E402
from examlops.serving.substrates.resolve import RenderError, ResolvedRef  # noqa: E402

_REF = ResolvedRef("m", "1", "Production", "s3://b/k", "unsigned", "default")


@pytest.mark.parametrize("framework", sorted(kserve.FRAMEWORK_RUNTIMES))
def test_every_mapped_framework_renders_valid_with_an_explicit_runtime(framework):
    m = kserve.render_inference_service({"name": "m", "framework": framework}, _REF)
    model = m["spec"]["predictor"]["model"]
    fmt, runtime = kserve.FRAMEWORK_RUNTIMES[framework]
    assert model["modelFormat"] == {"name": fmt} and model["runtime"] == runtime
    assert ("protocolVersion" in model) == ("v2" in kserve.RUNTIME_PROTOCOLS[runtime])
    assert k8s_schema.validate(m) == []


def test_pytorch_is_pinned_to_triton_and_never_torchserve():
    m = kserve.render_inference_service({"name": "m", "framework": "pytorch"}, _REF)
    assert m["spec"]["predictor"]["model"]["runtime"] == "kserve-tritonserver"
    assert all(rt != "kserve-torchserve" for _fmt, rt in kserve.FRAMEWORK_RUNTIMES.values())


@pytest.mark.parametrize("framework", ["", "caffe", None])
def test_an_unmapped_framework_is_refused_not_rendered_without_a_format(framework):
    with pytest.raises(RenderError, match="framework"):
        kserve.render_inference_service({"name": "m", "framework": framework}, _REF)


def test_every_mapped_runtime_has_a_recorded_protocol_set():
    for _fmt, runtime in kserve.FRAMEWORK_RUNTIMES.values():
        assert runtime in kserve.RUNTIME_PROTOCOLS
    assert "v2" not in kserve.RUNTIME_PROTOCOLS["kserve-tensorflow-serving"]


def test_an_llm_engine_other_than_vllm_is_refused_rather_than_mislabelled():
    with pytest.raises(RenderError, match="vLLM"):
        kserve.render_llm_inference_service({"name": "m", "engine": {"engine": "sglang"}}, _REF)
