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
    "KubeRayK8s",
    "RayServeCompose",
    "RenderError",
    "ResolvedRef",
    "ServingBackend",
    "registry_to_kserve",
    "registry_to_kserve_verified",
    "registry_to_kuberay",
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


def registry_to_kserve_verified(
    model_yaml: dict[str, Any],
    resolved: ResolvedRef,
    *,
    canary: ResolvedRef | None = None,
    canary_pct: int | None = None,
    verifier: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """:func:`registry_to_kserve` plus the in-pod verify-before-load wiring (ADR 0142 d3).

    ``verifier`` defaults to :meth:`VerifierSpec.from_env` — the same ``EXAMLOPS_SERVING_VERIFY``
    the Ray path obeys — so a previewed manifest is the one the ``kserve`` substrate would apply.
    Returns the manifest and the render's warnings; ``enforce`` refuses (``RenderError``) what it
    cannot verify.
    """
    from examlops.serving.substrates.verifier import VerifierSpec, attach_verifier

    spec = verifier if verifier is not None else VerifierSpec.from_env()
    manifest = registry_to_kserve(model_yaml, resolved, canary=canary, canary_pct=canary_pct)
    return attach_verifier(manifest, resolved, spec, canary=canary)


def registry_to_kuberay(
    registry_dir: str,
    *,
    image: str,
    name: str | None = None,
    project: str = "default",
    min_workers: int = 1,
    max_workers: int = 4,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render the registry's Ray multi-model server as a KubeRay ``RayService`` (ADR 0015 d1).

    Validated against the vendored KubeRay CRD schema; ``env`` defaults to the caller's
    environment, filtered to :data:`kuberay.PASSTHROUGH_ENV` (never a credential).
    """
    from examlops.serving.substrates import kuberay

    manifest = kuberay.render_ray_service(
        kuberay.models_in(registry_dir),
        image=image,
        name=name or kuberay.DEFAULT_NAME,
        project=project,
        env=dict(os.environ) if env is None else env,
        min_workers=min_workers,
        max_workers=max_workers,
    )
    errors = k8s_schema.validate(manifest)
    if errors:
        raise RenderError(f"render failed the pinned KubeRay schema: {errors}")
    return manifest


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

    Every manifest carries the in-pod verify-before-load wiring when it is configured
    (:mod:`examlops.serving.substrates.verifier`, ADR 0142 d3); :meth:`verify_before_load` is the
    same D3 rule for an in-process loader.
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
        wired, warnings = registry_to_kserve_verified(model_yaml, ref)
        manifest = self._checked(wired, f"KServe manifest for {model}")
        return {
            "backend": self.name,
            "manifest": manifest,
            "warnings": warnings,
            "applied": _kubectl_apply(manifest),
        }

    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]:
        model_yaml = self._load_yaml(model)
        stable = self._resolve(model, model_yaml, alias="Production")
        canary = self._resolve(model, model_yaml, alias=alias)
        wired, warnings = registry_to_kserve_verified(
            model_yaml, stable, canary=canary, canary_pct=canary_pct
        )
        manifest = self._checked(wired, f"canary manifest for {model}")
        return {
            "backend": self.name,
            "manifest": manifest,
            "warnings": warnings,
            "applied": _kubectl_apply(manifest),
        }

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


class KubeRayK8s:
    """KubeRay backend — the Ray multi-model server as a ``RayService`` (ADR 0015 d1).

    Render-and-validate only, like :class:`KServeK8s`'s preview: nothing is applied. Every model in
    the registry is served by one Ray cluster, so ``deploy``/``rollout`` render the whole service;
    the alias split itself stays the Ray router's (``exa serve traffic``), exactly as on Compose.
    """

    name = "kuberay-k8s"

    def __init__(self, registry_dir: str | None = None, image: str | None = None) -> None:
        from examlops.usecase import models_dir

        self.registry_dir = registry_dir or os.getenv("RAY_MODELS_DIR") or str(models_dir())
        self.image = image or os.getenv("EXAMLOPS_KUBERAY_IMAGE") or ""

    def _render(self) -> dict[str, Any]:
        if not self.image:
            raise RenderError("set EXAMLOPS_KUBERAY_IMAGE to the pinned ray-serving image")
        return registry_to_kuberay(self.registry_dir, image=self.image)

    def deploy(self, model: str, version: str, alias: str) -> dict[str, Any]:
        return {"backend": self.name, "model": model, "manifest": self._render(), "applied": "no"}

    def rollout(self, model: str, alias: str, canary_pct: int) -> dict[str, Any]:
        # The split is a Ray router rule, not a second object: the same as the Compose path.
        return {
            "backend": self.name,
            "model": model,
            "alias": alias,
            "canary_pct": canary_pct,
            "via": "exa serve traffic (Ray router weights)",
        }

    def status(self, model: str) -> dict[str, Any]:
        return {"backend": self.name, "model": model, "path": "kuberay"}


_BACKENDS: dict[str, Any] = {
    "ray-compose": RayServeCompose,
    "kserve-k8s": KServeK8s,
    "kuberay-k8s": KubeRayK8s,
}


def select_backend(name: str | None = None) -> ServingBackend:
    """Select the serving backend from the arg or ``EXAMLOPS_SERVING_BACKEND`` (default compose)."""
    chosen = (name or os.getenv("EXAMLOPS_SERVING_BACKEND") or "ray-compose").lower()
    cls = _BACKENDS.get(chosen, RayServeCompose)
    backend: ServingBackend = cls()
    return backend
