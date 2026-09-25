"""Verify-before-load inside a KServe pod (ADR 0142 d3, spec-usar-1 R-SUB-20, ADR 0015 d5).

KServe has no model-signature hook, so the D3 check has to run *in the pod*, after the artifact is
downloaded and before the model server starts. Where that can happen was read from the pinned
release's own source (v0.20.0), not assumed:

* a plain user ``initContainer`` runs **too early** — the pod webhook *appends* the
  ``storage-initializer`` after the user's init containers
  (``pkg/webhook/admission/pod/storage_initializer_injector.go``), so a verifier there would check
  an empty ``/mnt/models``;
* an ``InferenceService`` predictor can instead name a ``ClusterStorageContainer``
  (``spec.predictor.storageContainerName``), and the webhook then runs **that** container as the
  storage initializer. The platform ships one (:func:`render_storage_container`) whose image
  downloads with KServe's own ``kserve_storage`` and then runs the D3 verifier
  (:mod:`examlops.supplychain.pod_verifier`) — one container, download *then* verify, so no ordering
  question remains. The per-model inputs reach it through the downward API from predictor
  annotations, because a cluster-scoped storage container cannot carry per-model values;
* an ``LLMInferenceService`` has no ``storageContainerName``; its controller instead *merges* a
  user ``template.initContainers[name=storage-initializer]`` into the one it builds, keeping the
  user's image and env but forcing its own args
  (``pkg/controller/v1alpha2/llmisvc/workload_storage.go``, ``attachStorageInitializer``). So the
  same image is named there, with the per-model values as literal env.

Mode is the serving path's own switch, ``EXAMLOPS_SERVING_VERIFY`` (``off`` | ``warn`` default |
``enforce``), so the Compose path and the Kubernetes path answer to one setting. ``enforce`` is
fail-closed at every step it can be: an unknown mode, a missing verifier image, or a storage scheme
no storage initializer downloads (``pvc://``, ``oci://`` — nothing would run the verifier) refuses the
*render* rather than producing a pod that loads unverified bytes; in the pod, a failed **or
unanswerable** verification exits non-zero, so the model server never starts.

Everything here is pure: the :class:`VerifierSpec` is read from the environment once, by the
substrate's constructor, and the rest is a function of its inputs.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import Any

from examlops.serving.substrates.resolve import RenderError, ResolvedRef, scheme_of

__all__ = [
    "ANN_MODE",
    "ANN_MODEL",
    "ANN_SIGNATURE",
    "ANN_VERSION",
    "DOWNLOAD_SCHEMES",
    "VERIFY_MODES",
    "VerifierSpec",
    "attach_verifier",
    "render_storage_container",
]

VERIFY_MODES = ("off", "warn", "enforce")

# Schemes a KServe storage initializer downloads into /mnt/models — and so the ones a verifier in
# that container can check. ``pvc://`` is mounted, ``oci://`` is a modelcar sidecar: no download
# step runs, so there is nowhere to put the check (usr-02 §2; injector v0.20.0).
DOWNLOAD_SCHEMES = ("s3", "gs", "hdfs", "webhdfs", "http", "https", "hf")

ANN_MODE = "examlops.io/verify-mode"
ANN_MODEL = "examlops.io/verify-model"
ANN_VERSION = "examlops.io/verify-version"
ANN_SIGNATURE = "examlops.io/signature"

DEFAULT_STORAGE_CONTAINER = "examlops-verified-storage"
DEFAULT_TRUST_CONFIGMAP = "examlops-signing-trust"
TRUST_CONFIGMAP_KEY = "public-keys"
STORAGE_INITIALIZER = "storage-initializer"  # KServe's own container name (pkg/constants)


@dataclass(frozen=True)
class VerifierSpec:
    """How (and whether) a KServe render carries the in-pod verifier."""

    mode: str = "warn"
    image: str | None = None
    storage_container: str = DEFAULT_STORAGE_CONTAINER
    trust_configmap: str = DEFAULT_TRUST_CONFIGMAP

    def __post_init__(self) -> None:
        if self.mode not in VERIFY_MODES:
            raise RenderError(
                f"EXAMLOPS_SERVING_VERIFY={self.mode!r} is not one of {', '.join(VERIFY_MODES)}; "
                "an unrecognised mode is refused rather than read as 'off'"
            )

    @classmethod
    def from_env(cls) -> VerifierSpec:
        return cls(
            mode=(os.getenv("EXAMLOPS_SERVING_VERIFY") or "warn").strip().lower(),
            image=(os.getenv("EXAMLOPS_KSERVE_VERIFIER_IMAGE") or "").strip() or None,
            storage_container=(
                os.getenv("EXAMLOPS_KSERVE_STORAGE_CONTAINER") or DEFAULT_STORAGE_CONTAINER
            ).strip(),
            trust_configmap=(
                os.getenv("EXAMLOPS_KSERVE_TRUST_CONFIGMAP") or DEFAULT_TRUST_CONFIGMAP
            ).strip(),
        )


def _annotations(ref: ResolvedRef, mode: str) -> dict[str, str]:
    return {
        ANN_MODE: mode,
        ANN_MODEL: ref.model,
        ANN_VERSION: ref.version,
        ANN_SIGNATURE: ref.signature or "",
    }


def _trust_env(spec: VerifierSpec) -> dict[str, Any]:
    # Optional: a missing trust ConfigMap leaves the bundle empty, which fails every Ed25519
    # check ("untrusted-key") — the pod still refuses in enforce mode, it does not skip.
    return {
        "name": "EXAMLOPS_SIGNING_PUBLIC_KEYS",
        "valueFrom": {
            "configMapKeyRef": {
                "name": spec.trust_configmap,
                "key": TRUST_CONFIGMAP_KEY,
                "optional": True,
            }
        },
    }


def _algo(record_json: str | None) -> str:
    """The algorithm a public signature record names (``hmac-sha256`` when it names none)."""
    try:
        record = json.loads(record_json or "")
    except ValueError:
        return "unreadable"
    if not isinstance(record, dict):
        return "unreadable"
    return str(record.get("algo") or "hmac-sha256")


def _usable(spec: VerifierSpec, refs: list[ResolvedRef], warnings: list[str]) -> bool:
    """Whether the verifier can be rendered for ``refs``; refuses (enforce) or warns (warn)."""
    if spec.mode == "off":
        warnings.append(
            "verify-before-load is off (EXAMLOPS_SERVING_VERIFY=off): the pod loads whatever the "
            "storage initializer downloads"
        )
        return False
    problems: list[str] = []
    if not spec.image:
        problems.append(
            "no verifier image (set EXAMLOPS_KSERVE_VERIFIER_IMAGE and install the storage "
            "container: `exa serve manifest --target storage-container`)"
        )
    for ref in refs:
        scheme = scheme_of(ref.artifact_uri)
        if scheme not in DOWNLOAD_SCHEMES:
            problems.append(
                f"{ref.artifact_uri!r}: KServe runs no storage initializer for {scheme}://, so "
                "nothing in the pod could verify it"
            )
    if not problems:
        for ref in refs:
            if not ref.signature:
                warnings.append(
                    f"{ref.model} v{ref.version} has no signature on record: the pod will "
                    + (
                        "refuse to start (enforce)"
                        if spec.mode == "enforce"
                        else "start and log it (warn)"
                    )
                )
            elif _algo(ref.signature) != "ed25519-v2":
                warnings.append(
                    f"{ref.model} v{ref.version} is signed with {_algo(ref.signature)!r}, which a "
                    "pod cannot verify without the shared key: the pod will "
                    + (
                        "refuse to start (enforce)"
                        if spec.mode == "enforce"
                        else "start and log it (warn)"
                    )
                    + "; re-sign with Ed25519"
                )
        return True
    if spec.mode == "enforce":
        raise RenderError(
            "verify-before-load cannot be rendered in enforce mode: " + "; ".join(problems)
        )
    warnings.extend(f"verify-before-load not rendered: {p}" for p in problems)
    return False


def attach_verifier(
    obj: dict[str, Any],
    ref: ResolvedRef,
    spec: VerifierSpec,
    *,
    canary: ResolvedRef | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of the KServe ``obj`` wired to verify its artifact in the pod, plus warnings.

    ``canary`` is the canary predictor's own version on an ``InferenceService`` (it downloads its
    own bytes, so it is checked against its own record).
    """
    warnings: list[str] = []
    refs = [ref] + ([canary] if canary is not None else [])
    if not _usable(spec, refs, warnings):
        return obj, warnings
    out = copy.deepcopy(obj)
    kind = out.get("kind")
    if kind == "InferenceService":
        predictor = out["spec"]["predictor"]
        predictor["storageContainerName"] = spec.storage_container
        predictor.setdefault("annotations", {}).update(_annotations(ref, spec.mode))
        for entry in out["spec"].get("canary") or []:
            if canary is None:
                raise RenderError("an InferenceService canary needs its resolved version")
            cp = entry["predictor"]
            cp["storageContainerName"] = spec.storage_container
            cp.setdefault("annotations", {}).update(_annotations(canary, spec.mode))
    elif kind == "LLMInferenceService":
        values = _annotations(ref, spec.mode)
        out["spec"].setdefault("annotations", {}).update(values)
        env = [
            {"name": "EXAMLOPS_VERIFY_MODE", "value": values[ANN_MODE]},
            {"name": "EXAMLOPS_VERIFY_MODEL", "value": values[ANN_MODEL]},
            {"name": "EXAMLOPS_VERIFY_VERSION", "value": values[ANN_VERSION]},
            {"name": "EXAMLOPS_VERIFY_RECORD", "value": values[ANN_SIGNATURE]},
            _trust_env(spec),
        ]
        template = out["spec"].setdefault("template", {})
        inits = [
            c for c in template.get("initContainers") or [] if c.get("name") != STORAGE_INITIALIZER
        ]
        inits.append({"name": STORAGE_INITIALIZER, "image": spec.image, "env": env})
        template["initContainers"] = inits
    else:
        raise RenderError(f"verify-before-load has no rendering for kind {kind!r}")
    return out, warnings


def render_storage_container(spec: VerifierSpec) -> dict[str, Any]:
    """The cluster-scoped ``ClusterStorageContainer`` that runs download-then-verify.

    Installed once per cluster (it is cluster-scoped, so it needs cluster-admin; the
    ``InferenceService`` objects that name it do not). It matches every download scheme, so an
    ``InferenceService`` that does not name it may still be served by it when the webhook
    auto-matches; the verifier is a pass-through for a pod that carries no
    ``examlops.io/verify-mode`` annotation — it verifies only what the platform rendered.
    """
    if not spec.image:
        raise RenderError(
            "the storage container needs the verifier image: set EXAMLOPS_KSERVE_VERIFIER_IMAGE"
        )

    def from_annotation(env: str, key: str) -> dict[str, Any]:
        return {
            "name": env,
            "valueFrom": {"fieldRef": {"fieldPath": f"metadata.annotations['{key}']"}},
        }

    return {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ClusterStorageContainer",
        "metadata": {
            "name": spec.storage_container,
            "labels": {"app.kubernetes.io/managed-by": "examlops"},
        },
        "spec": {
            "workloadType": "initContainer",
            "supportedUriFormats": [{"prefix": f"{s}://"} for s in DOWNLOAD_SCHEMES],
            "container": {
                "name": STORAGE_INITIALIZER,
                "image": spec.image,
                "env": [
                    from_annotation("EXAMLOPS_VERIFY_MODE", ANN_MODE),
                    from_annotation("EXAMLOPS_VERIFY_MODEL", ANN_MODEL),
                    from_annotation("EXAMLOPS_VERIFY_VERSION", ANN_VERSION),
                    from_annotation("EXAMLOPS_VERIFY_RECORD", ANN_SIGNATURE),
                    _trust_env(spec),
                ],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            },
        },
    }
