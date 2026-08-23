"""D3 — ML supply-chain security: signing, AI-BOM, verify-before-load (ADR 0013).

Sign a model artifact bundle, emit a CycloneDX AI-BOM, and verify signature +
integrity before serving. The production signer is Sigstore/OpenSSF model-signing
(keyless Fulcio/OIDC); the **fallback** — used with no OIDC identity — is an
HMAC-SHA256 signature keyed by a D7-managed signing key, so the feature works with
no external service.

Verify-before-load (spec R6/R7): ``enforce`` mode refuses to load a version whose
signature/integrity check fails; ``warn`` mode records + alerts but may load.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_CHUNK = 1 << 20
# Per-digest verify cache (spec R8).
_verify_cache: dict[str, bool] = {}


class SigningKeyMissing(RuntimeError):
    """No signing key available (D7 secret or EXAMLOPS_SIGNING_KEY)."""


@dataclass(frozen=True)
class Signature:
    model: str
    version: str
    digest: str
    algo: str
    signature: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    digest: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _signing_key() -> bytes:
    """Resolve the HMAC signing key from D7 secrets or the env fallback."""
    key = os.getenv("EXAMLOPS_SIGNING_KEY")
    if not key:
        try:
            from examlops.secrets import get_secret

            key = get_secret("model-signing/key", actor="supplychain")
        except Exception:
            key = None
    if not key:
        raise SigningKeyMissing(
            "no signing key: set EXAMLOPS_SIGNING_KEY or store secret 'model-signing/key'"
        )
    return key.encode() if isinstance(key, str) else key


def artifact_digest(paths: Iterable[Path]) -> str:
    """SHA-256 over the sorted artifact file set (bundle digest)."""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        if not p.is_file():
            continue
        h.update(p.name.encode())
        with p.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
    return h.hexdigest()


def _hmac_sign(digest: str) -> str:
    return hmac.new(_signing_key(), digest.encode(), hashlib.sha256).hexdigest()


def sign_model(
    model: str, version: str, artifact_paths: Iterable[Path], *, actor: str | None = None
) -> Signature:
    """Sign a model artifact bundle and store the signature (spec R1/R2)."""
    from examlops.data.registry import store_model_signature

    digest = artifact_digest(artifact_paths)
    sig = _hmac_sign(digest)
    store_model_signature(model, version, digest, sig, algo="hmac-sha256", signed_by=actor)
    _verify_cache.pop(digest, None)
    _audit("model_signed", model, version, actor, {"digest": digest[:16]})
    return Signature(model, version, digest, "hmac-sha256", sig)


def verify_model(model: str, version: str, artifact_paths: Iterable[Path]) -> VerifyResult:
    """Verify a model's signature against the current artifact bytes (spec R6, cached R8)."""
    from examlops.data.registry import get_model_signature

    row = get_model_signature(model, version)
    if row is None:
        return VerifyResult(False, "unsigned: no signature on record")
    current = artifact_digest(artifact_paths)
    if current in _verify_cache:
        ok = _verify_cache[current]
        return VerifyResult(ok, "cached" if ok else "cached-fail", current)
    if current != row["digest"]:
        _verify_cache[current] = False
        return VerifyResult(False, "tampered: artifact digest changed", current)
    expected = _hmac_sign(current)
    ok = hmac.compare_digest(expected, row["signature"])
    _verify_cache[current] = ok
    return VerifyResult(ok, "verified" if ok else "bad-signature", current)


def verify_before_load(
    model: str, version: str, artifact_paths: Iterable[Path], *, mode: str = "enforce"
) -> bool:
    """Gate model loading on verification (spec R7).

    ``enforce`` → return False (refuse) on failure; ``warn`` → return True but record.

    A verification that cannot *run* counts as a failure, not as a pass. Deciding this needs two
    reads that can fail on their own account — the recorded signature comes from the datastore and
    the digest comes from the artifact files on disk — and when either raises, the question "do
    these bytes match their signature?" has no answer. Letting the exception escape pushed that
    decision onto whichever caller was least equipped to make it: the KServe loader hook answered
    it with ``return True``.
    """
    try:
        result = verify_model(model, version, artifact_paths)
    except Exception as exc:  # noqa: BLE001 - any failure to verify is a failure to verify
        _audit(
            "model_verify_error",
            model,
            version,
            "serving",
            {"reason": f"verification could not run: {exc}", "mode": mode},
        )
        return mode != "enforce"
    if result.ok:
        return True
    _audit(
        "model_verify_failed", model, version, "serving", {"reason": result.reason, "mode": mode}
    )
    return mode != "enforce"  # warn may still load; enforce refuses


def generate_ai_bom(
    model: str,
    version: str,
    *,
    dataset: str | None = None,
    dataset_revision: str | None = None,
    framework: str | None = None,
    eval_summary: dict | None = None,
    dependencies: list[str] | None = None,
) -> dict:
    """Emit a CycloneDX-style AI-BOM for a model version (spec R3/R4)."""
    from examlops.data.registry import store_model_bom

    deps = dependencies or _key_dependency_versions()
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "timestamp": _now(),
            "component": {"type": "machine-learning-model", "name": model, "version": str(version)},
        },
        "components": [
            {
                "type": "data",
                "name": dataset or "unknown",
                "version": dataset_revision or "unknown",
            },
            *[
                {"type": "library", "name": d.split("==")[0], "version": d.split("==")[-1]}
                for d in deps
            ],
        ],
        "properties": [
            {"name": "examlops:framework", "value": framework or "unknown"},
            {"name": "examlops:dataset_revision", "value": dataset_revision or "unknown"},
            {"name": "examlops:eval", "value": str(eval_summary or {})},
        ],
    }
    store_model_bom(model, version, bom)
    return bom


def _key_dependency_versions() -> list[str]:
    out = []
    for pkg in ("mlflow", "scikit-learn", "torch", "numpy", "pandas"):
        try:
            import importlib.metadata as md

            out.append(f"{pkg}=={md.version(pkg)}")
        except Exception:
            continue
    return out


def _audit(action: str, model: str, version: str, actor: str | None, extra: dict) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-supplychain", actor, action, f"{model}@{version}", extra)
    except Exception:
        pass
