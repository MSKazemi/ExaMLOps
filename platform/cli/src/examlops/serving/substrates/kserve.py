"""Render servables as KServe objects for the pinned release (ADR 0142 d2/d3, spec-usar-1 §4.2–4.4).

* ``predictive`` → ``serving.kserve.io/v1beta1`` ``InferenceService`` in **Standard** mode, with a
  ``modelFormat`` and a pinned ``runtime`` taken from the framework table below;
* ``generative`` → ``serving.kserve.io/v1alpha2`` ``LLMInferenceService`` — ``spec.model{uri,name}``,
  the llm-d router with KServe-managed defaults, and the vLLM argv from the one shared renderer
  (:func:`examlops.engines.config.to_vllm_args`). There is no ``spec.predictor`` on this kind.

Every function here is pure: the :class:`~.resolve.ResolvedRef` carries everything that needed the
registry, so the same inputs always produce the same object.
"""

from __future__ import annotations

import re
from typing import Any

from examlops.serving.substrates.resolve import RenderError, ResolvedRef

ISVC_API = "serving.kserve.io/v1beta1"
LLMISVC_API = "serving.kserve.io/v1alpha2"
DEPLOYMENT_MODE_ANNOTATION = "serving.kserve.io/deploymentMode"  # pkg/constants @ v0.20.0
STANDARD_MODE = "Standard"  # "RawDeployment" is the deprecated alias

# framework (model YAML) → (modelFormat.name, runtime). Verified against KServe v0.20.0
# config/runtimes/*.yaml (supportedModelFormats + autoSelect). The runtime is always pinned so the
# object says what will serve it. `pytorch` goes to Triton, which lists the format with
# autoSelect: false — left unpinned, KServe would auto-select TorchServe, which is archived
# upstream with no security patches (usr-02 §2). Triton needs a TorchScript/ONNX export.
FRAMEWORK_RUNTIMES: dict[str, tuple[str, str]] = {
    "sklearn": ("sklearn", "kserve-sklearnserver"),
    "xgboost": ("xgboost", "kserve-xgbserver"),
    "lightgbm": ("lightgbm", "kserve-lgbserver"),
    "mlflow": ("mlflow", "kserve-mlserver"),
    "pyfunc": ("mlflow", "kserve-mlserver"),
    "onnx": ("onnx", "kserve-tritonserver"),
    "tensorrt": ("tensorrt", "kserve-tritonserver"),
    "tensorflow": ("tensorflow", "kserve-tritonserver"),
    "pytorch": ("pytorch", "kserve-tritonserver"),
    "huggingface": ("huggingface", "kserve-huggingfaceserver"),
}

# runtime → protocolVersions, from the same runtime definitions at v0.20.0.
RUNTIME_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "kserve-sklearnserver": ("v1", "v2"),
    "kserve-xgbserver": ("v1", "v2"),
    "kserve-lgbserver": ("v1", "v2"),
    "kserve-mlserver": ("v2",),
    "kserve-tritonserver": ("v2", "grpc-v2"),
    "kserve-huggingfaceserver": ("v2", "v1"),
    "kserve-tensorflow-serving": ("v1", "grpc-v1"),
    "kserve-torchserve": ("v1", "v2", "grpc-v2"),
}

_FORBIDDEN_RUNTIMES = {"kserve-torchserve": "TorchServe is archived upstream (no security patches)"}
_GENERATIVE_ENGINES = ("vllm", "sglang")


def is_generative(model_yaml: dict[str, Any]) -> bool:
    task = str(model_yaml.get("task_type", "")).lower()
    engine = (model_yaml.get("engine") or {}).get("engine")
    return "llm" in task or "generation" in task or engine in _GENERATIVE_ENGINES


def service_name(model: str) -> str:
    """A DNS-1035 service name for ``model`` (lower-case; ``_``/``.`` become ``-``)."""
    name = re.sub(r"[_.]+", "-", model.strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", name) or len(name) > 63:
        raise RenderError(f"model name {model!r} cannot become a Kubernetes service name")
    return name


def _metadata(ref: ResolvedRef, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "labels": {
            "examlops.io/model": ref.model,
            "examlops.io/version": ref.version,
            "examlops.io/alias": ref.alias or "none",
            "examlops.io/project": ref.project,
            "app.kubernetes.io/managed-by": "examlops",
        },
        "annotations": {"examlops.io/artifact-digest": ref.digest},
    }


def format_and_runtime(model_yaml: dict[str, Any]) -> tuple[str, str]:
    framework = str(model_yaml.get("framework") or "").strip().lower()
    if framework not in FRAMEWORK_RUNTIMES:
        raise RenderError(
            f"framework {framework or '(missing)'!r} has no KServe runtime mapping "
            f"(known: {', '.join(sorted(FRAMEWORK_RUNTIMES))})"
        )
    fmt, runtime = FRAMEWORK_RUNTIMES[framework]
    if runtime in _FORBIDDEN_RUNTIMES:  # pragma: no cover - the table never maps to one
        raise RenderError(f"runtime {runtime} refused: {_FORBIDDEN_RUNTIMES[runtime]}")
    return fmt, runtime


_CONTAINER = "kserve-container"  # KServe's own name for the model-server container


def _variant(ref: ResolvedRef) -> str:
    """A DNS-1035 predictor name derived from the version (``17`` → ``v17``)."""
    return service_name(f"v{ref.version}")


def _predictor(model_yaml: dict[str, Any], ref: ResolvedRef) -> dict[str, Any]:
    fmt, runtime = format_and_runtime(model_yaml)
    model: dict[str, Any] = {
        "modelFormat": {"name": fmt},
        "runtime": runtime,
        "storageUri": ref.artifact_uri,
    }
    # Claim the Open Inference Protocol only where the runtime actually speaks it (R-SUB-10).
    if "v2" in RUNTIME_PROTOCOLS.get(runtime, ()):
        model["protocolVersion"] = "v2"
    return {"model": model}


def render_inference_service(
    model_yaml: dict[str, Any],
    ref: ResolvedRef,
    *,
    canary: ResolvedRef | None = None,
    canary_pct: int | None = None,
) -> dict[str, Any]:
    """A predictive servable as a Standard-mode ``InferenceService``.

    A canary uses the v0.20 Standard-mode ``spec.canary[]`` list: the older
    ``canaryTrafficPercent`` is a Knative-mode field and would be a silent no-op here.
    """
    name = service_name(str(model_yaml.get("name") or ref.model))
    meta = _metadata(ref, name)
    meta["annotations"][DEPLOYMENT_MODE_ANNOTATION] = STANDARD_MODE
    spec: dict[str, Any] = {"predictor": _predictor(model_yaml, ref)}
    if canary is not None or canary_pct is not None:
        if canary is None or canary_pct is None:
            raise RenderError("a canary needs both a resolved canary version and a percent")
        if not 0 <= int(canary_pct) <= 100:
            raise RenderError(f"canary percent {canary_pct} is outside 0..100")
        if canary.version == ref.version:
            raise RenderError(f"canary version {canary.version} is the stable version")
        # v0.20.0: a canary's predictor.name names its Deployment ({isvc}-{name}-predictor) and its
        # model needs a container name; naming the stable predictor the same way is what lets a
        # promotion rename the stable to the canary's name without a restart (inference_service.go).
        spec["predictor"]["name"] = _variant(ref)
        canary_predictor = _predictor(model_yaml, canary)
        canary_predictor["name"] = _variant(canary)
        canary_predictor["model"]["name"] = _CONTAINER
        spec["canary"] = [{"predictor": canary_predictor, "trafficPercent": int(canary_pct)}]
        meta["annotations"]["examlops.io/canary-version"] = canary.version
        meta["annotations"]["examlops.io/canary-artifact-digest"] = canary.digest
    return {"apiVersion": ISVC_API, "kind": "InferenceService", "metadata": meta, "spec": spec}


def render_llm_inference_service(model_yaml: dict[str, Any], ref: ResolvedRef) -> dict[str, Any]:
    """A generative servable as an ``LLMInferenceService`` (v1alpha2, the storage version)."""
    from examlops.engines.config import EngineConfig, to_vllm_args

    engine = EngineConfig.from_dict(model_yaml.get("engine") or {})
    if engine.engine != "vllm":
        raise RenderError(
            f"engine {engine.engine!r}: an LLMInferenceService is rendered from vLLM flags "
            "(to_vllm_args) at this pin; another engine needs its own renderer"
        )
    name = service_name(str(model_yaml.get("name") or ref.model))
    main: dict[str, Any] = {"name": "main"}
    args = to_vllm_args(engine)
    if args:
        main["args"] = args
    spec: dict[str, Any] = {
        "model": {"uri": ref.artifact_uri, "name": engine.served_model_name or ref.model},
        # Empty blocks select KServe's managed defaults (the v0.20.0 llmisvc samples do the same):
        # the llm-d scheduler, an HTTPRoute and the default Gateway.
        "router": {"scheduler": {}, "route": {}, "gateway": {}},
        "template": {"containers": [main]},
    }
    parallelism = {
        key: size
        for key, size in (
            ("tensor", engine.tensor_parallel_size),
            ("pipeline", engine.pipeline_parallel_size),
            ("data", engine.data_parallel_size),
        )
        if size > 1
    }
    if parallelism:
        spec["parallelism"] = parallelism
    return {
        "apiVersion": LLMISVC_API,
        "kind": "LLMInferenceService",
        "metadata": _metadata(ref, name),
        "spec": spec,
    }


def render_llm_inference_service_canary(
    model_yaml: dict[str, Any], ref: ResolvedRef, canary: ResolvedRef, canary_pct: int
) -> list[dict[str, Any]]:
    """A generative canary as **two** ``LLMInferenceService`` objects (ADR 0142 d5, spec-usar-1
    §5.6): stable and canary each render independently (they may run different engine configs,
    same as the predictor pair on ISVC), then share ``spec.router.route.group`` and split
    ``weight`` 100-p / p — there is no single object with a nested canary list here, unlike ISVC
    Standard mode, because ``LLMInferenceService`` has no ``spec.canary`` field at this pin.
    """
    if not 0 <= int(canary_pct) <= 100:
        raise RenderError(f"canary percent {canary_pct} is outside 0..100")
    if canary.version == ref.version:
        raise RenderError(f"canary version {canary.version} is the stable version")
    group = service_name(str(model_yaml.get("name") or ref.model))
    stable_obj = render_llm_inference_service(model_yaml, ref)
    canary_obj = render_llm_inference_service(model_yaml, canary)
    canary_obj["metadata"]["name"] = service_name(f"{group}-canary")
    canary_obj["metadata"]["annotations"]["examlops.io/canary-version"] = canary.version
    canary_obj["metadata"]["annotations"]["examlops.io/canary-artifact-digest"] = canary.digest
    stable_obj["spec"]["router"]["route"] = {"group": group, "weight": 100 - int(canary_pct)}
    canary_obj["spec"]["router"]["route"] = {"group": group, "weight": int(canary_pct)}
    return [stable_obj, canary_obj]


def render(
    model_yaml: dict[str, Any],
    ref: ResolvedRef,
    *,
    canary: ResolvedRef | None = None,
    canary_pct: int | None = None,
) -> dict[str, Any]:
    """Render ``model_yaml`` for KServe, choosing the kind from the servable."""
    if is_generative(model_yaml):
        if canary is not None or canary_pct is not None:
            raise RenderError(
                "an LLMInferenceService canary is two services sharing router.route.group "
                "(spec-usar-1 §5.6) — not rendered by this command yet"
            )
        return render_llm_inference_service(model_yaml, ref)
    return render_inference_service(model_yaml, ref, canary=canary, canary_pct=canary_pct)
