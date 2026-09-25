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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops import data as platform_db

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

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
    warnings: list[str] = field(default_factory=list)


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


def _git_dirty() -> bool | None:
    """True when tracked files differ from ``HEAD``; ``None`` when it cannot be determined.

    Untracked files are ignored on purpose: they cannot change what a commit contains, and a
    fresh checkout would not have them either way. A ``None`` is recorded as-is — never as
    ``False`` — so a bundle never claims a clean tree it did not observe.
    """
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return bool(done.stdout.strip())


MAX_PACKAGES = 5000  # bound on the recorded package set (manifest size stays small)


def capture_packages() -> dict[str, str]:
    """``name -> version`` of the current interpreter's installed distributions (``pip freeze``).

    Names are lower-cased with ``_``/``.`` folded to ``-`` (PEP 503) so the same distribution
    compares equal however it was spelled. Sorted and capped at :data:`MAX_PACKAGES`.
    """
    from importlib import metadata

    seen: dict[str, str] = {}
    for dist in metadata.distributions():
        try:
            name = dist.metadata["Name"]
            if not name:
                continue
            key = re.sub(r"[-_.]+", "-", name).lower()
            seen.setdefault(key, dist.version)
        except Exception:  # noqa: BLE001 - one broken dist-info must not stop the capture
            continue
    return dict(sorted(seen.items())[:MAX_PACKAGES])


def compare_packages(
    recorded: dict[str, str], current: dict[str, str] | None = None
) -> dict[str, Any]:
    """Per-package drift of ``recorded`` against ``current`` (default: this interpreter).

    ``changed`` (version differs) and ``missing`` (recorded but not installed) are drift.
    ``added`` (installed but not recorded) is reported and is not drift: an extra distribution
    cannot make a recorded one differ.
    """
    now = capture_packages() if current is None else current
    changed = {k: (v, now[k]) for k, v in recorded.items() if k in now and now[k] != v}
    missing = sorted(k for k in recorded if k not in now)
    added = sorted(k for k in now if k not in recorded)
    return {
        "changed": changed,
        "missing": missing,
        "added": added,
        "drift": bool(changed or missing),
    }


def describe_package_drift(cmp: dict[str, Any], limit: int = 6) -> str:
    """One bounded line naming the drifted packages."""
    parts = [f"{k} {a}->{b}" for k, (a, b) in list(cmp["changed"].items())[:limit]]
    parts += [f"{k} missing" for k in cmp["missing"][: max(0, limit - len(parts))]]
    total = len(cmp["changed"]) + len(cmp["missing"])
    more = f" (+{total - len(parts)} more)" if total > len(parts) else ""
    return f"{total} package(s) differ: " + ", ".join(parts) + more


def verify_dataplane_snapshot(source_key: str, revision: str, *, store: Any = None) -> str | None:
    """Verify a dataplane snapshot against its manifest; ``None`` when it verifies, else why not.

    This is the same check training runs before it reads a snapshot: the revision id is
    resolved in the store, the manifest's file digests must hash back to it (content address),
    and every part is downloaded and checked against its sha256 (``dataplane.materialize``).
    Nothing is reported verified unless that whole path succeeded. The download goes to a
    throw-away directory, so verifying leaves no cache behind.
    """
    import tempfile

    try:
        from examlops.dataplane import materialize, resolve, store_from_env
        from examlops.dataplane.types import DataplaneError

        st = store if store is not None else store_from_env()
        try:
            ref = resolve(st, source_key, revision)
            with tempfile.TemporaryDirectory(prefix="exa-repro-snap-") as tmp:
                materialize(st, ref, Path(tmp))
        except DataplaneError as exc:
            return f"snapshot {source_key}@{revision[:16]} does not verify: {exc}"
    except Exception as exc:  # noqa: BLE001 - an unreachable store is a failure, not a pass
        return f"cannot verify snapshot {source_key}@{revision[:16]}: {exc}"
    return None


def _dataplane_source(manifest: dict[str, Any]) -> str | None:
    src = manifest.get("dataset_source") or {}
    if isinstance(src, dict) and src.get("kind") == "dataplane" and src.get("source_key"):
        return str(src["source_key"])
    return None


def _code_repo(inp: dict[str, Any]) -> str | None:
    """Where to look for a code input's commit: ``None`` = the current checkout.

    The model library is looked up in ``EXAMLOPS_MODELZOO_DIR`` when set (it may live somewhere
    else on the verifying host), else at the path recorded when the bundle was built.
    """
    if not str(inp.get("ref") or "").startswith("modelzoo@"):
        return inp.get("repo")
    import os

    return (os.getenv("EXAMLOPS_MODELZOO_DIR") or "").strip() or inp.get("repo")


def verify_lakefs_revision(row: dict[str, Any]) -> str | None:
    """``None`` when a lakeFS-kind revision's commit still exists in lakeFS, else why not."""
    from examlops.reproducibility import lakefs

    parsed = lakefs.parse_uri(row.get("uri"))
    if parsed is None:
        return f"lakeFS revision has no lakefs:// uri ({row.get('uri')!r})"
    repo, commit = parsed
    if commit != row.get("revision_id"):
        return f"lakeFS uri names commit {commit[:16]}, revision is {row.get('revision_id')}"
    return lakefs.verify_commit(repo, commit)


def _git_commit_exists(sha: str, repo: str | None = None) -> bool:
    prefix = ["git", "-C", repo] if repo else ["git"]
    try:
        return (
            subprocess.run(
                [*prefix, "cat-file", "-e", f"{sha}^{{commit}}"],
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
    """Sign the manifest hash with the D3 HMAC key; degrade to unsigned when none is configured.

    A signing *failure* is logged rather than recorded as though it were policy — see
    :func:`examlops.supplychain.sign_or_explain`.
    """
    from examlops.supplychain import sign_or_explain

    return sign_or_explain(manifest_hash, subject=f"reproducibility bundle {manifest_hash[:12]}")


def build_bundle(
    model: str,
    version: str,
    *,
    dataset_revision: str | None = None,
    dataset_name: str | None = None,
    feature_views: dict[str, Any] | list[str] | None = None,
    hyperparams: dict[str, Any] | None = None,
    seeds: dict[str, int] | None = None,
    resources: dict[str, Any] | None = None,
    hardware: dict[str, Any] | None = None,
    image_digest: str | None = None,
    lineage_run_id: str | None = None,
    metrics: dict[str, float] | None = None,
    tolerance: dict[str, Any] | None = None,
    actor: str | None = None,
    dataset_source: dict[str, Any] | None = None,
    trigger: str | None = None,
    run_spec: dict[str, Any] | None = None,
    bom: bool = True,
) -> Bundle:
    """Capture, hash, and sign a reproducibility manifest for a model version (R1/R2/GWT-1).

    Inputs the caller does not supply are *collected* (:mod:`examlops.reproducibility.capture`):
    the platform and model-library commits, the host hardware, the container image digest
    (``EXAMLOPS_IMAGE_DIGEST``) and the feature views named in ``EXAMLOPS_FEATURE_VIEWS``.
    ``feature_views`` given as a list of names is resolved to definition hashes; an unknown name
    raises ``KeyError``. ``bom`` links the version's D3 AI-BOM (generated from the recorded
    package set when the version has none) and records its hash.
    """
    from examlops.reproducibility import capture as cap

    lock_hash, lock_path = _env_lock_hash()
    commit = _git_commit()
    dirty = _git_dirty()
    packages = capture_packages()
    code_commits = cap.capture_code_commits()
    if hardware is None:
        hardware = cap.capture_hardware()
    # A hardware record that says "not captured" means this process is not the host that
    # trained (a promotion, a job a scheduler placed elsewhere). Its own EXAMLOPS_IMAGE_DIGEST
    # — the promoting CLI's or control plane's image — is then no more the training image than
    # its CPUs are the training CPUs; recording it would make `--execute` demand the wrong image.
    if image_digest is None and (hardware or {}).get("captured") is not False:
        image_digest = cap.capture_image_digest()
    views: dict[str, Any]
    if feature_views is None:
        env_views = cap.feature_view_names_from_env()
        views = cap.capture_feature_views(env_views) if env_views else {}
    elif isinstance(feature_views, list):
        views = cap.capture_feature_views(feature_views)
    else:
        views = dict(feature_views)

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
    mz = code_commits.get("modelzoo") or {}
    if mz.get("commit"):
        inputs.append(
            {
                "kind": "code",
                "ref": f"modelzoo@{mz['commit']}",
                "hash": mz["commit"],
                "repo": mz.get("path"),
            }
        )
    for fv_name, fv in sorted(views.items()):
        fv_hash = str(fv.get("version") if isinstance(fv, dict) else fv)
        # Only a definition hash can be re-checked by `verify`. A caller-supplied counter (the
        # pre-ADR-0038 ``{"view": 3}`` form) stays in the manifest as recorded, but is not made
        # an input: it could never equal a definition hash, so every verify would report rot.
        if _SHA256_HEX.match(fv_hash):
            inputs.append({"kind": "feature_view", "ref": fv_name, "hash": fv_hash})
    if lineage_run_id:
        inputs.append({"kind": "lineage", "ref": lineage_run_id, "hash": lineage_run_id})
    bom_ref: dict[str, Any] | None = None
    if bom:
        try:
            bom_ref = cap.attach_bom(
                model,
                str(version),
                packages=packages,
                dataset_name=dataset_name,
                dataset_revision=dataset_revision,
            )
            inputs.append({"kind": "bom", "ref": f"{model}@{version}", "hash": bom_ref["sha256"]})
        except Exception as exc:  # noqa: BLE001 - recorded as a failure, never as a BOM
            bom_ref = {"sha256": None, "error": f"{type(exc).__name__}: {exc}"[:300]}

    manifest: dict[str, Any] = {
        "model": model,
        "version": str(version),
        "code_commit": commit,
        # True = tracked files differed from the commit when this bundle was built, so the
        # commit does NOT contain the code that ran; None = could not be determined.
        "code_dirty": dirty,
        # Every repository whose code the run executed (ADR 0038 cl. 1: "all relevant repos").
        "code_commits": code_commits,
        "dataset_name": dataset_name,
        "dataset_revision": dataset_revision,
        "dataset_source": dataset_source or {},
        "feature_views": views,
        "environment": {
            "lock_sha256": lock_hash,
            "lock_path": lock_path,
            "image_digest": image_digest,
            "python": sys.version.split()[0],
            "packages": packages,
        },
        "hyperparams": hyperparams or {},
        "seeds": seeds or {},
        "resources": resources or {},
        "hardware": hardware or {},
        "lineage_run_id": lineage_run_id,
        "bom": bom_ref,
        "metrics": metrics or {},
        "tolerance": tolerance or DEFAULT_TOLERANCE,
        "nondeterminism_caveat": NONDETERMINISM_CAVEAT,
        "inputs": inputs,
    }
    if trigger:
        manifest["trigger"] = trigger
    if run_spec:
        # How the pipeline was invoked: {"registry_model", "dataset", "backend"} — what
        # `--execute` needs to call training_flow again (the bundle's `model` is the MLflow id).
        manifest["run_spec"] = run_spec
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


def verify_bundle(model: str, version: str, *, allow_env_drift: bool = False) -> VerifyResult:
    """Check referenced inputs still exist + hashes match; flag rot (R5/GWT-4).

    A dataset pinned to a dataplane snapshot is verified against the snapshot manifest and its
    part checksums, not against the ``dataset_revisions`` table. The environment is checked at
    two levels: the lockfile hash, and — when the bundle recorded one — the package set against
    this interpreter. Package drift is a problem unless ``allow_env_drift``, then a warning.
    """
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
            repo = _code_repo(inp)
            ok = _git_commit_exists(inp["hash"], repo)
            reason = "" if ok else f"commit not reachable in git{f' ({repo})' if repo else ''}"
        elif kind == "dataset":
            rev = manifest.get("dataset_revision")
            name = manifest.get("dataset_name")
            plane = _dataplane_source(manifest)
            if plane and rev:
                why = verify_dataplane_snapshot(plane, str(rev))
                ok, reason = why is None, why or ""
            else:
                found = platform_db.get_dataset_revision(name, rev) if (name and rev) else None
                ok = found is not None
                reason = "" if ok else "dataset revision no longer recorded (purged?)"
                if found is not None and found.get("kind") == "lakefs":
                    why = verify_lakefs_revision(found)
                    ok, reason = why is None, why or ""
        elif kind == "feature_view":
            from examlops.data.data_assets import get_feature_view
            from examlops.reproducibility.capture import feature_view_version

            view = get_feature_view(str(inp.get("ref")))
            if view is None:
                ok, reason = False, "feature view no longer registered"
            else:
                ok = feature_view_version(view) == inp["hash"]
                reason = "" if ok else "feature view definition changed since the bundle"
        elif kind == "lineage":
            from examlops.data.events import lineage_run_seen

            ok = lineage_run_seen(str(inp["hash"]))
            reason = "" if ok else "A2 lineage run no longer recorded"
        elif kind == "bom":
            from examlops.reproducibility.capture import current_bom_sha256

            cur_bom = current_bom_sha256(model, str(version))
            ok = cur_bom is not None and cur_bom == inp["hash"]
            reason = "" if ok else ("AI-BOM missing" if cur_bom is None else "AI-BOM changed")
        elif kind == "env":
            cur = _file_sha256(Path(inp["ref"])) if inp.get("ref") else None
            ok = cur is not None and cur == inp["hash"]
            reason = "" if ok else "lockfile missing or changed"
        checked.append({"kind": kind, "ref": inp.get("ref"), "ok": ok, "reason": reason})
        if not ok:
            problems.append(f"{kind}: {reason}")

    warnings: list[str] = []
    if manifest.get("code_dirty"):
        warnings.append(
            "code was dirty when the bundle was built: the recorded commit does not contain "
            "the code that ran"
        )
    if ((manifest.get("code_commits") or {}).get("modelzoo") or {}).get("dirty"):
        warnings.append(
            "modelzoo was dirty when the bundle was built: its recorded commit does not contain "
            "the library code that ran"
        )
    recorded_pkgs = (manifest.get("environment") or {}).get("packages")
    if recorded_pkgs:
        cmp = compare_packages(recorded_pkgs)
        ok = not cmp["drift"]
        reason = "" if ok else describe_package_drift(cmp)
        if not ok and allow_env_drift:
            warnings.append(f"env packages: {reason} (--allow-env-drift)")
            ok = True
        checked.append(
            {"kind": "env_packages", "ref": "importlib.metadata", "ok": ok, "reason": reason}
        )
        if not ok:
            problems.append(f"env_packages: {reason}")

    return VerifyResult(
        model=model,
        version=str(version),
        reproducible=not problems,
        inputs=checked,
        problems=problems,
        warnings=warnings,
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
    from examlops.data.audit import audit_best_effort

    # Failing open, but counted: a lost record must show in dropped_audit_events().
    audit_best_effort("exa-reproduce", actor, action, f"{model}/{version}", extra)


def technical_evidence(model: str, version: str) -> dict[str, Any]:
    """Shape the bundle as D1 technical-doc / D2 evidence input (R6/GWT-5).

    Called by the D1 Annex-IV technical file and the D2 coverage report through the
    ``reproducibility`` evidence collector in :mod:`examlops.compliance`.
    """
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
        "bundle_version": row.get("bundle_version"),
        "reproducible": v.reproducible,
        "problems": list(v.problems),
        "code_commit": m.get("code_commit"),
        "code_commits": {
            k: (c or {}).get("commit") or (c or {}).get("distribution_version")
            for k, c in (m.get("code_commits") or {}).items()
        },
        "dataset_revision": m.get("dataset_revision"),
        "bom_sha256": (m.get("bom") or {}).get("sha256"),
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
    "capture_packages",
    "compare_packages",
    "verify_dataplane_snapshot",
    "reproduce",
    "technical_evidence",
    "verify_lakefs_revision",
]
