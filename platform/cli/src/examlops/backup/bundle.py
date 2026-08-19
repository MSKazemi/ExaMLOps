"""Bundle orchestration — create / verify / restore / list a whole-platform backup bundle.

A bundle is a directory ``examlops-backup-<UTC>/`` containing per-tier sub-dirs plus a top-level
``bundle.manifest.json`` that aggregates each tier's :class:`~examlops.backup._manifest.TierResult`,
records which tiers were requested, and rolls up an overall status. Every tier runs through
:func:`~examlops.backup._manifest.run_tier` so a heavy tier that can't run degrades to ``skipped``
(or, under ``strict``, fails loudly). The control-plane profile (``sqlite`` + ``config``) has zero
external dependencies and always succeeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config_tier, objects_tier, postgres_tier, sqlite_tier
from ._manifest import FAILED, OK, PARTIAL, SKIPPED, TierResult, run_tier

_MANIFEST_NAME = "bundle.manifest.json"
_BUNDLE_GLOB = "examlops-backup-*"

# Tier name → (callable(dest_dir) -> TierResult, needs a kwarg?). config takes with_content.
_ALL_TIERS = ("sqlite", "config", "postgres", "objects")


@dataclass
class BundleResult:
    bundle_id: str
    bundle_dir: str
    overall_status: str
    manifest: dict[str, Any]


def _examlops_version() -> str:
    try:
        from importlib.metadata import version

        return version("examlops")
    except Exception:  # noqa: BLE001
        return "unknown"


def _actor() -> str:
    import os

    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-backup"


def _overall_status(tier_results: list[TierResult]) -> str:
    statuses = [t.status for t in tier_results]
    if any(s == FAILED for s in statuses):
        return FAILED
    if any(s in (SKIPPED, PARTIAL) for s in statuses):
        # A skipped tier only makes the whole bundle "partial" if it was actually requested;
        # not-requested tiers are recorded but don't degrade the overall status.
        return PARTIAL if any(s in (OK, PARTIAL) for s in statuses) else SKIPPED
    return OK


def create_bundle(
    out_dir: str,
    *,
    tiers: list[str] | None = None,
    strict: bool = False,
    with_content: bool = False,
    profile: str | None = None,
) -> BundleResult:
    """Create a backup bundle under ``out_dir``. ``tiers`` defaults to the control-plane profile."""
    requested = set(tiers or ["sqlite", "config"])
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_id = f"examlops-backup-{ts}"
    bundle_dir = Path(out_dir) / bundle_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    results: list[TierResult] = [
        run_tier(
            "sqlite",
            lambda: sqlite_tier.backup_sqlite_tier(bundle_dir),
            requested="sqlite" in requested,
            strict=strict,
        ),
        run_tier(
            "config",
            lambda: config_tier.backup_config_tier(bundle_dir, with_content=with_content),
            requested="config" in requested,
            strict=strict,
        ),
        run_tier(
            "postgres",
            lambda: postgres_tier.backup_postgres_tier(bundle_dir),
            requested="postgres" in requested,
            strict=strict,
        ),
        run_tier(
            "objects",
            lambda: objects_tier.backup_objects_tier(bundle_dir),
            requested="objects" in requested,
            strict=strict,
        ),
    ]

    overall = _overall_status([r for r in results if r.name in requested])
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "created_by": f"exa-backup/{_actor()}",
        "examlops_version": _examlops_version(),
        "profile": profile or ("all" if requested >= set(_ALL_TIERS) else "custom"),
        "requested_tiers": sorted(requested),
        "strict": strict,
        "overall_status": overall,
        "tiers": {r.name: r.to_manifest() for r in results},
    }
    (bundle_dir / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return BundleResult(bundle_id, str(bundle_dir), overall, manifest)


def verify_bundle(bundle_dir: str) -> dict[str, Any]:
    """Verify a bundle: manifest present + every ``ok`` tier item's checksum still matches.

    Never raises; returns ``{"ok": bool, "reason": str, "checks": {...}}``.
    """
    d = Path(bundle_dir)
    mp = d / _MANIFEST_NAME
    if not mp.exists():
        return {"ok": False, "reason": "no bundle.manifest.json", "checks": {}}
    manifest = json.loads(mp.read_text())
    checks: dict[str, Any] = {}
    ok = True

    from ._manifest import sha256_file, sha256_text

    for tier_name, tier in manifest.get("tiers", {}).items():
        for item in tier.get("items", []):
            if item.get("status") != OK:
                continue
            label = item.get("name") or item.get("bucket") or item.get("file") or tier_name
            key = f"{tier_name}:{label}"
            if "file" in item:
                fp = d / item["file"]
                if not fp.exists():
                    checks[key] = "missing file"
                    ok = False
                elif "sha256" in item and sha256_file(fp) != item["sha256"]:
                    checks[key] = "sha256 mismatch"
                    ok = False
                else:
                    checks[key] = "ok"
            elif tier_name == "objects" and "dir" in item:
                idx = d / item["dir"] / "_index.json"
                if not idx.exists():
                    checks[key] = "missing index"
                    ok = False
                elif "sha256" in item and sha256_text(idx.read_text()) != item["sha256"]:
                    checks[key] = "index sha256 mismatch"
                    ok = False
                else:
                    checks[key] = "ok"

    # For the sqlite platform DB, also re-run integrity + audit-chain on the snapshot file.
    for item in manifest.get("tiers", {}).get("sqlite", {}).get("items", []):
        if item.get("status") == OK and item.get("name") == "platform" and "file" in item:
            # A checksum mismatch above already means the file is corrupt; skip the deep check
            # (it would raise on a malformed image). Otherwise run it, but never let it crash.
            if checks.get("sqlite:platform") == "sha256 mismatch":
                ok = False
                continue
            try:
                res = sqlite_tier._verify_db_file(str(d / item["file"]))  # noqa: SLF001 — same pkg
                checks["sqlite:platform:integrity"] = res["integrity_check"]
                checks["sqlite:platform:audit_chain"] = "ok" if res["audit_chain_ok"] else "BROKEN"
                if res["integrity_check"] != "ok" or not res["audit_chain_ok"]:
                    ok = False
            except Exception as exc:  # noqa: BLE001 — a malformed snapshot must fail, not crash
                checks["sqlite:platform:integrity"] = f"error: {exc}"
                ok = False

    return {"ok": ok, "reason": "verified" if ok else "verification failed", "checks": checks}


def restore_bundle(
    bundle_dir: str, *, tiers: list[str] | None = None, force: bool = False
) -> dict[str, Any]:
    """Restore selected tiers from a verified bundle. Verifies BEFORE touching anything."""
    d = Path(bundle_dir)
    result = verify_bundle(bundle_dir)
    if not result["ok"]:
        raise ValueError(f"refusing to restore an unverified bundle: {result['reason']}")

    manifest = json.loads((d / _MANIFEST_NAME).read_text())
    available = list(manifest.get("tiers", {}).keys())
    selected = set(tiers) if tiers else {t for t in available if t in ("sqlite", "config")}

    restored: dict[str, Any] = {}
    if "sqlite" in selected:
        restored["sqlite"] = sqlite_tier.restore_sqlite_tier(d, force=force)
    if "config" in selected:
        restored["config"] = config_tier.restore_config_tier(d)
    if "postgres" in selected:
        restored["postgres"] = postgres_tier.restore_postgres_tier(d, force=force)
    if "objects" in selected:
        restored["objects"] = objects_tier.restore_objects_tier(d, force=force)
    # A restore that failed and reported success is worse than one that raised: the operator
    # believes state is back. Any item that explicitly says `ok: False` is surfaced here so the
    # caller — and `exa backup restore-bundle`, which exits 1 on it — cannot miss it.
    failed = [
        {"tier": tier, **item}
        for tier, items in restored.items()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and item.get("ok") is False
    ]
    return {
        "bundle": bundle_dir,
        "restored_tiers": sorted(selected),
        "detail": restored,
        "failed": failed,
        "ok": not failed,
    }


def list_bundles(directory: str) -> list[dict[str, Any]]:
    """List bundles in ``directory`` (newest first) with their manifest summary."""
    d = Path(directory)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for bd in sorted(d.glob(_BUNDLE_GLOB), reverse=True):
        mp = bd / _MANIFEST_NAME
        if not mp.is_file():
            continue
        m = json.loads(mp.read_text())
        out.append(
            {
                "bundle_id": m.get("bundle_id", bd.name),
                "path": str(bd),
                "created_at": m.get("created_at"),
                "overall_status": m.get("overall_status"),
                "profile": m.get("profile"),
                "tiers": list(m.get("tiers", {}).keys()),
            }
        )
    return out
