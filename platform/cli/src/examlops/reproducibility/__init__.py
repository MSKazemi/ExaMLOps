"""Next-Gen 40 · A8 — signed reproducibility bundles (ADR 0038).

A reproducibility **bundle** is a signed manifest capturing *every input* to a model
version — code commit(s), A1 dataset revision, A3 feature-view versions, resolved
environment (uv lockfile hash + container image digest), hyperparameters, scheduler
resources + hardware, RNG seeds, and A2 lineage — keyed to the model version. From it:

- ``build_bundle`` captures + hashes + signs the manifest (D3 signing, D4 audit).
- ``reproduce`` documents the rebuild plan and, given re-observed metrics, verifies they
  match the recorded ones **within a documented tolerance** — it never claims bit-exactness
  it cannot guarantee (GPU/kernel non-determinism is called out explicitly).
- ``verify_bundle`` checks that referenced inputs still exist and their hashes match,
  flagging a bundle that has rotted (e.g. a purged dataset revision).

Signing degrades gracefully: with no signing key the bundle is stored **unsigned** and
marked as such rather than failing — the honesty requirement (R4) applies to signing too.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops import data as platform_db

# Documented default tolerance + the non-determinism caveat (R4/GWT-3).
DEFAULT_TOLERANCE = {"metric": "rmse", "rel": 0.05}
NONDETERMINISM_CAVEAT = (
    "Metrics are verified within a relative tolerance, not bit-for-bit. GPU kernel "
    "scheduling, non-deterministic cuDNN ops, and reduction order make exact "
    "reproduction unattainable in general; this bundle does not claim bit-exactness."
)


@dataclass
class Bundle:
    model: str
    version: str
    manifest: dict[str, Any]
    manifest_hash: str
    signature: str | None
    signed: bool
    bundle_version: int


@dataclass
class VerifyResult:
    model: str
    version: str
    reproducible: bool
    inputs: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class ReproResult:
    model: str
    version: str
    verify: VerifyResult
    metric_match: bool | None  # None when no re-observed metrics were supplied
    details: list[str] = field(default_factory=list)
    bit_exact: bool = False  # always False — honesty (R4/GWT-3)


def _git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            or None
        )
    except Exception:
        return None


def _git_commit_exists(sha: str) -> bool:
    try:
        return (
            subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _env_lock_hash() -> tuple[str | None, str | None]:
    """(uv.lock sha256, path) from the repo root, if present."""
    for candidate in ("uv.lock", "requirements.txt"):
        p = Path(candidate)
        if p.is_file():
            return _file_sha256(p), str(p)
    return None, None


def _canonical_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _sign(manifest_hash: str) -> tuple[str | None, str | None]:
    """Sign the manifest hash with the D3 HMAC key; degrade to unsigned if no key."""
    try:
        from examlops.supplychain import _hmac_sign

        return _hmac_sign(manifest_hash), "hmac-sha256"
    except Exception:
        return None, None


def build_bundle(
    model: str,
    version: str,
    *,
    dataset_revision: str | None = None,
    dataset_name: str | None = None,
    feature_views: dict[str, int] | None = None,
    hyperparams: dict[str, Any] | None = None,
    seeds: dict[str, int] | None = None,
    resources: dict[str, Any] | None = None,
    hardware: dict[str, Any] | None = None,
    image_digest: str | None = None,
    lineage_run_id: str | None = None,
    metrics: dict[str, float] | None = None,
    tolerance: dict[str, Any] | None = None,
    actor: str | None = None,
) -> Bundle:
    """Capture, hash, and sign a reproducibility manifest for a model version (R1/R2/GWT-1)."""
    lock_hash, lock_path = _env_lock_hash()
    commit = _git_commit()

    inputs: list[dict[str, Any]] = []
    if commit:
        inputs.append({"kind": "code", "ref": commit, "hash": commit})
    if dataset_revision:
        inputs.append(
            {
                "kind": "dataset",
                "ref": f"{dataset_name or '?'}@{dataset_revision}",
                "hash": dataset_revision,
            }
        )
    if lock_hash:
        inputs.append({"kind": "env", "ref": lock_path, "hash": lock_hash})

    manifest: dict[str, Any] = {
        "model": model,
        "version": str(version),
        "code_commit": commit,
        "dataset_name": dataset_name,
        "dataset_revision": dataset_revision,
        "feature_views": feature_views or {},
        "environment": {
            "lock_sha256": lock_hash,
            "lock_path": lock_path,
            "image_digest": image_digest,
        },
        "hyperparams": hyperparams or {},
        "seeds": seeds or {},
        "resources": resources or {},
        "hardware": hardware or {},
        "lineage_run_id": lineage_run_id,
        "metrics": metrics or {},
        "tolerance": tolerance or DEFAULT_TOLERANCE,
        "nondeterminism_caveat": NONDETERMINISM_CAVEAT,
        "inputs": inputs,
    }
    manifest_hash = _canonical_hash(manifest)
    signature, algo = _sign(manifest_hash)
    bundle_version = platform_db.store_repro_bundle(
        model,
        version,
        manifest,
        manifest_hash,
        signature=signature,
        algo=algo,
        signed_by=actor,
    )
    _audit(
        model,
        version,
        "repro_bundle_built",
        {"hash": manifest_hash[:16], "signed": signature is not None},
        actor,
    )
    return Bundle(
        model=model,
        version=str(version),
        manifest=manifest,
        manifest_hash=manifest_hash,
        signature=signature,
        signed=signature is not None,
        bundle_version=bundle_version,
    )


def verify_bundle(model: str, version: str) -> VerifyResult:
    """Check referenced inputs still exist + hashes match; flag rot (R5/GWT-4)."""
    row = platform_db.get_repro_bundle(model, version)
    if not row:
        return VerifyResult(model, str(version), reproducible=False, problems=["no bundle found"])
    manifest = row["manifest"]

    # Integrity: does the stored manifest still hash to the recorded value?
    problems: list[str] = []
    recomputed = _canonical_hash(manifest)
    if recomputed != row["manifest_hash"]:
        problems.append("manifest hash mismatch (tampered or schema drift)")

    checked: list[dict[str, Any]] = []
    for inp in manifest.get("inputs", []):
        kind = inp["kind"]
        ok = True
        reason = ""
        if kind == "code":
            ok = _git_commit_exists(inp["hash"])
            reason = "" if ok else "commit not reachable in git"
        elif kind == "dataset":
            rev = manifest.get("dataset_revision")
            name = manifest.get("dataset_name")
            found = platform_db.get_dataset_revision(name, rev) if (name and rev) else None
            ok = found is not None
            reason = "" if ok else "dataset revision no longer recorded (purged?)"
        elif kind == "env":
            cur = _file_sha256(Path(inp["ref"])) if inp.get("ref") else None
            ok = cur is not None and cur == inp["hash"]
            reason = "" if ok else "lockfile missing or changed"
        checked.append({"kind": kind, "ref": inp.get("ref"), "ok": ok, "reason": reason})
        if not ok:
            problems.append(f"{kind}: {reason}")

    return VerifyResult(
        model=model,
        version=str(version),
        reproducible=not problems,
        inputs=checked,
        problems=problems,
    )


def _rel_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def reproduce(
    model: str,
    version: str,
    *,
    observed_metrics: dict[str, float] | None = None,
    actor: str | None = None,
) -> ReproResult:
    """Verify inputs, then metric-match re-observed metrics within tolerance (R3/GWT-2).

    Without ``observed_metrics`` (no re-run executed here) it returns the rebuild plan and
    input verification only. It never claims bit-exactness (R4/GWT-3).
    """
    verify = verify_bundle(model, version)
    row = platform_db.get_repro_bundle(model, version)
    details: list[str] = []
    metric_match: bool | None = None

    if row:
        manifest = row["manifest"]
        tol = manifest.get("tolerance", DEFAULT_TOLERANCE)
        details.append(f"checkout code {manifest.get('code_commit') or '(uncaptured)'}")
        details.append(
            f"restore dataset {manifest.get('dataset_name')}@{manifest.get('dataset_revision')}"
        )
        details.append(f"rebuild env from {manifest.get('environment', {}).get('lock_path')}")
        details.append(f"re-run with seeds {manifest.get('seeds')}")
        details.append(NONDETERMINISM_CAVEAT)

        if observed_metrics is not None:
            recorded = manifest.get("metrics", {})
            rel = float(tol.get("rel", 0.05))
            mismatches = []
            for k, v in recorded.items():
                if k in observed_metrics and _rel_diff(v, observed_metrics[k]) > rel:
                    mismatches.append(
                        f"{k}: recorded {v} vs observed {observed_metrics[k]} (>{rel:.0%})"
                    )
            metric_match = not mismatches
            details.extend(mismatches)

    _audit(
        model,
        version,
        "repro_attempt",
        {"reproducible": verify.reproducible, "metric_match": metric_match},
        actor,
    )
    return ReproResult(
        model=model,
        version=str(version),
        verify=verify,
        metric_match=metric_match,
        details=details,
        bit_exact=False,
    )


def _audit(model: str, version: str, action: str, extra: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-reproduce", actor, action, f"{model}/{version}", extra)
    except Exception:
        pass


def technical_evidence(model: str, version: str) -> dict[str, Any]:
    """Shape the bundle as D1 technical-doc / D2 evidence input (R6/GWT-5)."""
    row = platform_db.get_repro_bundle(model, version)
    if not row:
        return {"model": model, "version": str(version), "present": False}
    v = verify_bundle(model, version)
    m = row["manifest"]
    return {
        "model": model,
        "version": str(version),
        "present": True,
        "signed": row.get("signature") is not None,
        "manifest_hash": row["manifest_hash"],
        "reproducible": v.reproducible,
        "code_commit": m.get("code_commit"),
        "dataset_revision": m.get("dataset_revision"),
        "captured_inputs": [i["kind"] for i in m.get("inputs", [])],
    }


__all__ = [
    "Bundle",
    "VerifyResult",
    "ReproResult",
    "DEFAULT_TOLERANCE",
    "NONDETERMINISM_CAVEAT",
    "build_bundle",
    "verify_bundle",
    "reproduce",
    "technical_evidence",
]
