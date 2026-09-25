"""Resolve a servable to exactly what will run — before anything is rendered (ADR 0142 d1/d3).

A manifest that names ``mlflow://models/jpcp@Production`` names nothing a cluster can load (no
KServe storage initializer reads ``mlflow://``) and says nothing about *which* version it serves:
the alias moves, the object in the cluster does not. Resolution therefore happens once, here, and
is the only registry I/O on the render path; renderers take the resulting :class:`ResolvedRef` and
stay pure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Storage schemes the pinned KServe storage initializer / LLMInferenceService accept
# (usr-02 §2: gs s3 hdfs webhdfs http(s) pvc hf modelscope oci file). ``file://`` is excluded on
# purpose: a path on the machine that rendered the manifest is not a path inside the pod.
STORAGE_SCHEMES = ("s3", "gs", "hdfs", "webhdfs", "http", "https", "pvc", "hf", "oci")

# Registry-internal schemes: meaningful to MLflow, unreadable by a model server.
_REGISTRY_SCHEMES = ("mlflow", "models", "runs")
_PROXIED_PREFIX = "mlflow-artifacts:/"

UNSIGNED = "unsigned"


class RenderError(ValueError):
    """A servable cannot be rendered truthfully (unresolvable URI, unmapped framework, bad canary)."""


@dataclass(frozen=True)
class ResolvedRef:
    """An immutable answer to "what exactly runs": a concrete version and where its bytes are.

    ``digest`` is ``sha256:<hex>`` when a D3 signature is on record, else the literal
    ``"unsigned"`` — an unknown digest is represented, never invented (P5).

    ``signature`` is the public part of that D3 record (algo, digest, signature, key id) as
    canonical JSON, or ``None`` when nothing is on record. It is what the in-pod verifier checks
    the downloaded bytes against (ADR 0142 d3, R-SUB-20): a pod has no ``platform.db`` to read,
    so the record travels in the rendered object. Nothing in it is secret — an Ed25519 signature
    and a key id verify only against a trust bundle the pod is given separately.
    """

    model: str
    version: str
    alias: str | None
    artifact_uri: str
    digest: str = UNSIGNED
    project: str = "default"
    signature: str | None = None


def scheme_of(uri: str) -> str:
    return uri.split(":", 1)[0].lower() if ":" in uri else ""


def storage_uri(uri: str) -> str:
    """Return ``uri`` in a form a KServe pod can load, or raise :class:`RenderError`.

    ``mlflow-artifacts:/…`` is MLflow's proxied-artifact scheme; it maps onto real storage only
    through the server's ``--artifacts-destination``, which the platform learns from
    ``EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION``. Without it the URI is refused, not guessed.
    """
    if uri.startswith(_PROXIED_PREFIX):
        dest = os.getenv("EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION", "").strip()
        if not dest:
            raise RenderError(
                f"{uri!r} is an MLflow-proxied artifact URI; set "
                "EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION to the tracking server's "
                "--artifacts-destination (e.g. s3://mlflow-artifacts) or pass --artifact-uri"
            )
        uri = dest.rstrip("/") + "/" + uri[len(_PROXIED_PREFIX) :].lstrip("/")
    scheme = scheme_of(uri)
    if scheme in _REGISTRY_SCHEMES:
        raise RenderError(
            f"{uri!r} is a registry reference, not storage; resolve it to the version's artifact URI"
        )
    if scheme not in STORAGE_SCHEMES:
        raise RenderError(
            f"{uri!r} uses scheme {scheme or '(none)'!r}, which a KServe pod cannot load "
            f"(expected one of: {', '.join(STORAGE_SCHEMES)})"
        )
    return uri


def signature_digest(model: str, version: str) -> str:
    """``sha256:<hex>`` from the D3 signature store, or ``"unsigned"``."""
    try:
        from examlops.data.registry import get_model_signature
    except ImportError:  # pragma: no cover - the data layer ships with the package
        return UNSIGNED
    for name in dict.fromkeys((model, model.lower(), model.upper())):
        try:
            row = get_model_signature(name, str(version))
        except Exception:  # noqa: BLE001 - an unreadable store is "no signature on record"
            return UNSIGNED
        if row and row.get("digest"):
            return f"sha256:{row['digest']}"
    return UNSIGNED


# The columns of a ``model_signatures`` row a verifier needs; everything else (who signed, when)
# is provenance for the audit trail, not input to the check.
_SIGNATURE_FIELDS = ("algo", "digest", "signature", "cert")


def signature_record(model: str, version: str) -> str | None:
    """The public D3 record for ``model``/``version`` as canonical JSON, or ``None``."""
    try:
        from examlops.data.registry import get_model_signature
    except ImportError:  # pragma: no cover - the data layer ships with the package
        return None
    try:
        row = get_model_signature(model, str(version))
    except Exception:  # noqa: BLE001 - an unreadable store is "no signature on record"
        return None
    if not row or not row.get("digest") or not row.get("signature"):
        return None
    import json

    public = {k: row.get(k) for k in _SIGNATURE_FIELDS if row.get(k) is not None}
    return json.dumps(public, sort_keys=True, separators=(",", ":"))


def resolve_ref(
    model: str,
    *,
    alias: str | None = "Production",
    version: str | None = None,
    artifact_uri: str | None = None,
    project: str = "default",
    client: Any = None,
) -> ResolvedRef:
    """Resolve ``model`` (by alias, or an explicit version) to a :class:`ResolvedRef`.

    ``artifact_uri`` renders offline — no registry call — and therefore requires ``version``:
    a URI with no version would produce labels that name nothing. Otherwise the MLflow
    registry answers both questions (``client`` is injectable for tests).
    """
    name = model.lower()
    if artifact_uri is not None:
        if not version:
            raise RenderError(
                "--artifact-uri needs --version: the manifest must name what it serves"
            )
        return ResolvedRef(
            name,
            str(version),
            alias,
            storage_uri(artifact_uri),
            signature_digest(model, version),
            project,
            signature_record(model, str(version)),
        )
    if client is None:
        import mlflow

        client = mlflow.MlflowClient()
    if version is None:
        if not alias:
            raise RenderError("either an alias or a version is required to resolve a model")
        version = str(client.get_model_version_by_alias(name, alias).version)
    uri = client.get_model_version_download_uri(name, str(version))
    return ResolvedRef(
        name,
        str(version),
        alias,
        storage_uri(str(uri)),
        signature_digest(model, str(version)),
        project,
        signature_record(model, str(version)),
    )
