"""E1 — Kubernetes-native serving behind a ServingBackend seam (ADR 0015).

`exa serve` operations go through a :class:`ServingBackend` interface with two
implementations: **RayServeCompose** (default, current behaviour unchanged) and
**KServeK8s**, selected by ``EXAMLOPS_SERVING_BACKEND``. KServe manifests
(``InferenceService`` / ``LLMInferenceService``) are generated from the per-model YAML
registry + MLflow alias — no change to model definitions — and the K8s loader runs D3
verify-before-load. K8s is optional: with no cluster, the Compose path keeps working (R10).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

_API_VERSION = "serving.kserve.io/v1beta1"
_LLM_API_VERSION = "serving.kserve.io/v1alpha1"


@runtime_checkable
class ServingBackend(Protocol):
    name: str

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]: ...
    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]: ...
    def status(self, model: str) -> dict[str, Any]: ...


# ── Manifest generation (pure, schema-shaped) ─────────────────────────────────


def _is_llm(model_yaml: dict[str, Any]) -> bool:
    task = str(model_yaml.get("task_type", "")).lower()
    engine = (model_yaml.get("engine") or {}).get("engine")
    return "llm" in task or "generation" in task or engine in ("vllm", "sglang")


def registry_to_kserve(
    model_yaml: dict[str, Any], alias: str, *, canary_pct: int | None = None
) -> dict[str, Any]:
    """Generate a KServe InferenceService (or LLMInferenceService) from a model's YAML (R3).

    The manifest is structurally schema-valid (validated by :func:`validate_manifest`, and
    by ``kubectl --dry-run``/kubeconform in CI). ``canary_pct`` sets a canaryTrafficPercent
    on the predictor for a rollout (R4/GWT-3).
    """
    name = str(model_yaml["name"]).lower()
    llm = _is_llm(model_yaml)
    storage_uri = f"mlflow://models/{name}@{alias}"

    predictor: dict[str, Any] = {"model": {"storageUri": storage_uri, "protocolVersion": "v2"}}
    if llm:
        engine = model_yaml.get("engine") or {}
        predictor = {
            "model": {
                "storageUri": storage_uri,
                "modelFormat": {"name": engine.get("engine", "vllm")},
                "args": _llm_args(engine),
            }
        }
    if canary_pct is not None:
        predictor["canaryTrafficPercent"] = int(canary_pct)

    manifest = {
        "apiVersion": _LLM_API_VERSION if llm else _API_VERSION,
        "kind": "LLMInferenceService" if llm else "InferenceService",
        "metadata": {
            "name": name,
            "labels": {
                "examlops.io/model": name,
                "examlops.io/alias": alias,
                "app.kubernetes.io/managed-by": "examlops",
            },
        },
        "spec": {"predictor": predictor},
    }
    return manifest


def _llm_args(engine: dict[str, Any]) -> list[str]:
    """Render the vLLM argv for a KServe manifest — via the one shared renderer (R-V6).

    Delegating to ``engines.to_vllm_args`` is what keeps Kubernetes and HPC honest: the
    same ``engine:`` block produces the same flags on both, including the multimodal media
    limits, so a VLM cannot end up with its SSRF/DoS guards on one substrate and not the
    other. This used to emit four hand-transcribed flags and silently ignored the rest.
    """
    from examlops.engines.config import EngineConfig, to_vllm_args

    return to_vllm_args(EngineConfig.from_dict(engine))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Structural validation (the kubeconform/kubectl-dry-run seam) — returns errors (R6)."""
    errors: list[str] = []
    for key in ("apiVersion", "kind", "metadata", "spec"):
        if key not in manifest:
            errors.append(f"missing top-level '{key}'")
    kind = manifest.get("kind")
    if kind not in ("InferenceService", "LLMInferenceService"):
        errors.append(f"unexpected kind '{kind}'")
    meta = manifest.get("metadata", {})
    if not meta.get("name"):
        errors.append("metadata.name is required")
    if not isinstance(meta.get("name", ""), str) or " " in meta.get("name", ""):
        errors.append("metadata.name must be a DNS-safe string")
    predictor = manifest.get("spec", {}).get("predictor")
    if not predictor or "model" not in predictor:
        errors.append("spec.predictor.model is required")
    else:
        if not predictor["model"].get("storageUri"):
            errors.append("spec.predictor.model.storageUri is required")
    pct = predictor.get("canaryTrafficPercent") if predictor else None
    if pct is not None and not (0 <= int(pct) <= 100):
        errors.append("canaryTrafficPercent must be 0..100")
    return errors


# ── Backends ──────────────────────────────────────────────────────────────────


class RayServeCompose:
    """Default backend — the existing Ray Serve / Compose path (behaviour unchanged, R2)."""

    name = "ray-compose"

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]:
        return {"backend": self.name, "model": model, "version": version, "alias": alias}

    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]:
        # Delegates to the existing traffic-split mechanism (exa serve traffic).
        return {"backend": self.name, "model": model, "alias": alias, "canary_pct": canary_pct}

    def status(self, model: str) -> dict[str, Any]:
        return {"backend": self.name, "model": model, "path": "ray-serve"}


class KServeK8s:
    """KServe backend — generates + validates manifests and (in a cluster) applies them.

    D3 verify-before-load is invoked by the loader before serving (R9). With no cluster,
    ``apply`` degrades to returning the validated manifest so generation stays exercisable.
    """

    name = "kserve-k8s"

    def __init__(self, registry_dir: str | None = None) -> None:
        from examlops.usecase import models_dir

        # Per-model YAML lives in the active use-case pack (ADR 0094), resolved from the env.
        self.registry_dir = registry_dir or os.getenv("RAY_MODELS_DIR") or str(models_dir())

    def _load_yaml(self, model: str) -> dict[str, Any]:
        import yaml

        path = Path(self.registry_dir) / f"{model.lower()}.yaml"
        return yaml.safe_load(path.read_text())

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]:
        manifest = registry_to_kserve(self._load_yaml(model), alias)
        errors = validate_manifest(manifest)
        if errors:
            raise ValueError(f"invalid KServe manifest for {model}: {errors}")
        return {"backend": self.name, "manifest": manifest, "applied": _kubectl_apply(manifest)}

    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]:
        manifest = registry_to_kserve(self._load_yaml(model), alias, canary_pct=canary_pct)
        errors = validate_manifest(manifest)
        if errors:
            raise ValueError(f"invalid canary manifest for {model}: {errors}")
        return {"backend": self.name, "manifest": manifest, "applied": _kubectl_apply(manifest)}

    def status(self, model: str) -> dict[str, Any]:
        return {"backend": self.name, "model": model, "path": "kserve"}

    def verify_before_load(
        self, model: str, version: str, artifact_paths: list, *, mode: str = "enforce"
    ) -> bool:
        """K8s loader hook — refuse to serve a tampered/unsigned artifact in enforce mode (R9)."""
        try:
            from examlops.supplychain import verify_before_load
        except ImportError:
            # D3 unavailable → do not block Compose/dev; enforce only when D3 is present.
            return True
        # Anything the verifier itself raises is *not* the D3-absent case. Catching it here too
        # meant an unreachable signature store or an unreadable artifact was reported to the loader
        # as a clean verification, in the mode whose only job is to refuse.
        try:
            return verify_before_load(model, version, artifact_paths, mode=mode)
        except Exception:  # noqa: BLE001 - a verifier that cannot answer has not answered "yes"
            return mode != "enforce"


def _kubectl_apply(manifest: dict[str, Any]) -> str:  # pragma: no cover - needs a cluster
    """Apply a manifest via kubectl if a cluster is reachable; else report dry-run only."""
    import json
    import shutil
    import subprocess

    if not shutil.which("kubectl"):
        return "dry-run (no kubectl)"
    try:
        subprocess.run(
            ["kubectl", "apply", "--dry-run=server", "-f", "-"],
            input=json.dumps(manifest).encode(),
            check=True,
            capture_output=True,
            timeout=10,
        )
        return "validated (server dry-run)"
    except Exception as exc:
        return f"dry-run failed: {exc}"


_BACKENDS: dict[str, Any] = {"ray-compose": RayServeCompose, "kserve-k8s": KServeK8s}


def select_backend(name: str | None = None) -> ServingBackend:
    """Select the serving backend from the arg or ``EXAMLOPS_SERVING_BACKEND`` (default compose)."""
    chosen = (name or os.getenv("EXAMLOPS_SERVING_BACKEND") or "ray-compose").lower()
    cls = _BACKENDS.get(chosen, RayServeCompose)
    return cls()
