"""E1 — Kubernetes-native serving behind a ServingBackend seam (ADR 0015, refined by ADR 0142).

`exa serve` operations go through a :class:`ServingBackend` interface with two
implementations: **RayServeCompose** (default, current behaviour unchanged) and
**KServeK8s**, selected by ``EXAMLOPS_SERVING_BACKEND``. KServe objects are rendered by
:mod:`examlops.serving.substrates.kserve` from the per-model YAML **and a resolved, immutable
model version** (:mod:`examlops.serving.substrates.resolve`), and validated offline against the
pinned KServe CRD schemas (:mod:`examlops.serving.substrates.k8s_schema`). Applying stops at a
``kubectl --dry-run=server`` check: nothing is applied to a cluster yet (ADR 0142 d6, I3).
K8s is optional: with no cluster, the Compose path keeps working (R10).

This seam and :mod:`examlops.llm_endpoints` merge into one ``Substrate`` seam in USAR I1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from examlops.serving.substrates import k8s_schema, kserve
from examlops.serving.substrates.resolve import RenderError, ResolvedRef, resolve_ref

__all__ = [
    "KServeK8s",
    "RayServeCompose",
    "RenderError",
    "ResolvedRef",
    "ServingBackend",
    "registry_to_kserve",
    "select_backend",
    "validate_manifest",
]


@runtime_checkable
class ServingBackend(Protocol):
    name: str

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]: ...
    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]: ...
    def status(self, model: str) -> dict[str, Any]: ...


# ── Manifest generation ───────────────────────────────────────────────────────


def registry_to_kserve(
    model_yaml: dict[str, Any],
    resolved: ResolvedRef,
    *,
    canary: ResolvedRef | None = None,
    canary_pct: int | None = None,
) -> dict[str, Any]:
    """Render a model's YAML as a KServe ``InferenceService`` or ``LLMInferenceService``.

    ``resolved`` is the immutable version to serve (:func:`resolve_ref`). An alias string is
    refused: rendering one produced ``mlflow://`` URIs no KServe pod can load, naming a version
    that moves while the cluster object does not (ADR 0142 d3).
    """
    if not isinstance(resolved, ResolvedRef):
        raise TypeError(
            "registry_to_kserve needs a ResolvedRef (resolve the alias first with "
            "examlops.serving.substrates.resolve.resolve_ref), not "
            f"{type(resolved).__name__} {resolved!r}"
        )
    return kserve.render(model_yaml, resolved, canary=canary, canary_pct=canary_pct)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Structural errors against the pinned KServe CRD schema (empty = valid) — ADR 0142 d4."""
    return k8s_schema.validate(manifest)


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
    """KServe backend — resolves, renders and validates manifests; applies only as a dry run.

    :meth:`verify_before_load` is the D3 hook for a future loader; no rendered manifest invokes
    it yet (the verify-before-load init container is USAR I3, spec-usar-1 R-SUB-20).
    """

    name = "kserve-k8s"

    def __init__(self, registry_dir: str | None = None, client: Any = None) -> None:
        from examlops.usecase import models_dir

        # Per-model YAML lives in the active use-case pack (ADR 0094), resolved from the env.
        self.registry_dir = registry_dir or os.getenv("RAY_MODELS_DIR") or str(models_dir())
        self._client = client  # MLflow client; None ⇒ mlflow.MlflowClient() at resolve time

    def _load_yaml(self, model: str) -> dict[str, Any]:
        import yaml

        path = Path(self.registry_dir) / f"{model.lower()}.yaml"
        loaded: dict[str, Any] = yaml.safe_load(path.read_text())
        return loaded

    def _resolve(self, model: str, model_yaml: dict[str, Any], **kw: Any) -> ResolvedRef:
        project = str(model_yaml.get("project") or "default")
        return resolve_ref(model, project=project, client=self._client, **kw)

    def _checked(self, manifest: dict[str, Any], what: str) -> dict[str, Any]:
        errors = validate_manifest(manifest)
        if errors:
            raise ValueError(f"invalid {what}: {errors}")
        return manifest

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]:
        model_yaml = self._load_yaml(model)
        ref = self._resolve(model, model_yaml, alias=alias, version=version)
        manifest = self._checked(
            registry_to_kserve(model_yaml, ref), f"KServe manifest for {model}"
        )
        return {"backend": self.name, "manifest": manifest, "applied": _kubectl_apply(manifest)}

    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]:
        model_yaml = self._load_yaml(model)
        stable = self._resolve(model, model_yaml, alias="Production")
        canary = self._resolve(model, model_yaml, alias=alias)
        manifest = self._checked(
            registry_to_kserve(model_yaml, stable, canary=canary, canary_pct=canary_pct),
            f"canary manifest for {model}",
        )
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
    """Validate ``manifest`` with a *server-side dry run* when a cluster is reachable.

    Nothing is ever applied here: the real apply (plan-gated, audited) is USAR I3.
    """
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
    backend: ServingBackend = cls()
    return backend
