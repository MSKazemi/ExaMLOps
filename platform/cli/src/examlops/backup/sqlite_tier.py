"""SQLite backup tier — online, WAL-safe snapshots of every platform SQLite datastore.

The four legacy public functions (:func:`create_backup`, :func:`verify_backup`,
:func:`restore_backup`, :func:`list_backups`) are preserved **byte-for-byte in behaviour** — they
still operate on ``platform.db`` by default and are what ``exa backup create|verify|restore`` and
``tests/unit/test_backup_restore.py`` call. They were moved here unchanged from the old
``examlops.backup`` module and are re-exported from :mod:`examlops.backup`.

New in the tiered-bundle design: :func:`backup_sqlite_tier` snapshots *all* known SQLite DBs (keyed
by env var, not filename — the AGENT_* names are cross-wired) into a bundle sub-dir, degrading an
absent optional DB to ``skipped`` rather than failing. The audit hash-chain recompute only applies
to the DB that actually carries an ``audit_events`` table (``platform.db``).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from examlops.resilience import db as _rdb

from ._manifest import OK, SKIPPED, TierResult, rollup_status, sha256_file

_MANIFEST_SUFFIX = ".manifest.json"

# The SQLite datastores that make up the platform, keyed by env var (with its default path) so a
# relocated DB is still found. ``platform.db`` is resolved specially via ``platform_db._db_path()``.
_SQLITE_DBS: list[dict[str, str]] = [
    {"name": "platform", "env": "PLATFORM_DB", "default": ""},  # default resolved dynamically
    {"name": "approvals", "env": "CONTROL_PLANE_DB", "default": "/data/approvals.db"},
    {"name": "skipper_memory", "env": "AGENT_MEMORY_DB", "default": "./skipper_memory.db"},
    {"name": "agent_memory", "env": "AGENT_DB", "default": "./agent_memory.db"},
    # The Skipper memory-review queue (HITL-gated procedure writes) — instance data like the rest.
    {"name": "skipper_review", "env": "AGENT_MEMORY_REVIEW_DB", "default": "./skipper_review.db"},
    {"name": "mlflow", "env": "MLFLOW_SQLITE_DB", "default": "./mlflow.db"},
]


def _default_db_path() -> str:
    from examlops.platform_db import _db_path

    return _db_path()


def _platform_is_postgres() -> bool:
    return os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres"


def _resolve_db_path(spec: dict[str, str]) -> str:
    if spec["name"] == "platform":
        return os.getenv("PLATFORM_DB") or _default_db_path()
    if spec["env"].startswith("AGENT_") and not os.getenv(spec["env"]):
        # The agent's files follow the instance-data root like the agent itself does (ADR 0128).
        from examlops.lifecycle.datadir import agent_db_default

        return agent_db_default(Path(spec["default"]).name)
    return os.getenv(spec["env"], spec["default"])


# ── shared low-level helpers (moved verbatim from the old module) ──────────────


def _sha256(path: Path) -> str:
    return sha256_file(path)


def _table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for t in tables:
        # Table names come from sqlite_master (not user input); safe to interpolate.
        counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return counts


def _audit_head_hash(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _online_backup(src_path: str, dest_path: Path) -> None:
    """Copy ``src_path`` → ``dest_path`` as a transactionally-consistent SQLite snapshot."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = _rdb.connect(src_path)
    try:
        # Destination goes through the hardened connector too (Phase 0 item 0.2 guard); the
        # online backup API populates it via src.backup() regardless of its journal mode.
        dst = _rdb.connect(str(dest_path))
        try:
            src.backup(dst)  # atomic, consistent, WAL-safe
        finally:
            dst.close()
    finally:
        src.close()


def _snapshot_one(src: str, dest: Path) -> dict[str, Any]:
    """Snapshot one DB file into ``dest`` and return its manifest dict (not persisted here)."""
    _online_backup(src, dest)
    snap = _rdb.connect(str(dest))
    try:
        return {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(Path(src).resolve()),
            "backup_file": dest.name,
            "sha256": _sha256(dest),
            "size_bytes": dest.stat().st_size,
            "table_counts": _table_row_counts(snap),
            "audit_head_hash": _audit_head_hash(snap),
            "format": "sqlite-online-backup-v1",
        }
    finally:
        snap.close()


# ── legacy public API (unchanged signatures / return shapes) ───────────────────


def create_backup(out_dir: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Take an online snapshot of the platform DB into ``out_dir`` + write its manifest.

    Returns the manifest dict (also persisted to ``<backup>.manifest.json``).
    """
    src = db_path or _default_db_path()
    if not Path(src).exists():
        raise FileNotFoundError(f"platform DB not found at {src}")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(out_dir) / f"platform-{ts}.db"
    manifest = _snapshot_one(src, dest)
    _write_manifest(dest, manifest)
    return manifest


def _write_manifest(backup_path: Path, manifest: dict[str, Any]) -> None:
    import json

    (backup_path.parent / (backup_path.name + _MANIFEST_SUFFIX)).write_text(
        json.dumps(manifest, indent=2)
    )


def _read_manifest(backup_path: Path) -> dict[str, Any] | None:
    import json

    mp = backup_path.parent / (backup_path.name + _MANIFEST_SUFFIX)
    if not mp.exists():
        return None
    return json.loads(mp.read_text())


def _verify_db_file(path: str) -> dict[str, Any]:
    """Integrity + audit-chain verification of a SQLite file (no manifest involved).

    Recomputes the audit hash chain against the snapshot using the same canonicalisation the
    live DB uses, so a tampered/torn audit log is caught in the backup as it would be live.
    """
    conn = _rdb.connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        chain_ok = True
        chain_reason = "ok"
        try:
            rows = conn.execute(
                "SELECT id, source, actor, action, target, details, tenant, prev_hash, hash, ts "
                "FROM audit_events WHERE hash IS NOT NULL ORDER BY id ASC"
            ).fetchall()
            from examlops.platform_db import _audit_canonical, _audit_hash

            prev = "GENESIS"
            for r in rows:
                canonical = _audit_canonical(
                    r["source"],
                    r["actor"],
                    r["action"],
                    r["target"],
                    r["details"],
                    r["tenant"] or "default",
                    r["ts"],
                )
                if _audit_hash(prev, canonical) != r["hash"] or r["prev_hash"] != prev:
                    chain_ok = False
                    chain_reason = f"chain broken at id {r['id']}"
                    break
                prev = r["hash"]
        except sqlite3.Error:
            chain_reason = "no audit chain columns (pre-migration snapshot)"
    finally:
        conn.close()
    return {
        "integrity_check": integrity,
        "audit_chain_ok": chain_ok,
        "audit_chain_reason": chain_reason,
    }


def verify_backup(backup_path: str) -> dict[str, Any]:
    """Verify a backup is trustworthy: checksum matches the manifest + integrity + audit chain.

    Returns ``{"ok": bool, "reason": str, "checks": {...}}`` — never raises for a bad backup.
    """
    path = Path(backup_path)
    checks: dict[str, Any] = {}
    if not path.exists():
        return {"ok": False, "reason": "backup file not found", "checks": checks}

    manifest = _read_manifest(path)
    checks["manifest_present"] = manifest is not None
    if manifest is not None:
        checks["checksum_matches"] = _sha256(path) == manifest.get("sha256")
        if not checks["checksum_matches"]:
            return {"ok": False, "reason": "sha256 mismatch — backup corrupted", "checks": checks}

    checks.update(_verify_db_file(str(path)))
    ok = checks.get("integrity_check") == "ok" and checks.get("audit_chain_ok", True)
    return {"ok": ok, "reason": "verified" if ok else "verification failed", "checks": checks}


def _db_is_empty(path: str) -> bool:
    if not Path(path).exists():
        return True
    conn = _rdb.connect(path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if n == 0:
            return True
        # Has tables — empty only if every table has zero rows.
        for t in _table_row_counts(conn).values():
            if t > 0:
                return False
        return True
    finally:
        conn.close()


def restore_backup(
    backup_path: str, *, db_path: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Restore a verified backup over the platform DB (guarded + re-verified).

    Refuses to overwrite a non-empty target unless ``force=True``. Verifies the backup BEFORE
    touching the target, and re-verifies integrity + the audit chain AFTER restoring. Raises
    ``ValueError`` on a bad/unverified backup or an unforced overwrite.
    """
    src = Path(backup_path)
    dest = db_path or _default_db_path()

    result = verify_backup(backup_path)
    if not result["ok"]:
        raise ValueError(f"refusing to restore an unverified backup: {result['reason']}")

    if not _db_is_empty(dest) and not force:
        raise ValueError(
            f"target {dest} is non-empty — refusing to overwrite without force=True "
            "(back it up first, then retry with --force)"
        )

    # Restore via the online API into the target so WAL sidecar files are handled correctly.
    for sidecar in ("-wal", "-shm"):
        p = Path(dest + sidecar)
        if p.exists():
            p.unlink()
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    if Path(dest).exists():
        Path(dest).unlink()
    _online_backup(str(src), Path(dest))

    checks = _verify_db_file(dest)
    post_ok = checks["integrity_check"] == "ok" and checks["audit_chain_ok"]
    if not post_ok:
        raise ValueError(f"restore completed but the restored DB failed verification: {checks}")
    return {"restored_to": str(Path(dest).resolve()), "verification": {"ok": True, **checks}}


def list_backups(directory: str) -> list[dict[str, Any]]:
    """List backups in ``directory`` (newest first) with their manifest metadata."""
    d = Path(directory)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(d.glob("platform-*.db"), reverse=True):
        manifest = _read_manifest(f) or {}
        out.append(
            {
                "file": f.name,
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "created_at": manifest.get("created_at"),
                "sha256": manifest.get("sha256"),
                "has_manifest": bool(manifest),
            }
        )
    return out


# ── tiered-bundle API ──────────────────────────────────────────────────────────


def backup_sqlite_tier(dest_dir: Path) -> TierResult:
    """Snapshot every known platform SQLite DB into ``dest_dir/sqlite/``.

    An absent optional DB is recorded as ``skipped`` (not an error) — a fresh install may not yet
    have an approvals or agent DB. The platform DB missing entirely is still just ``skipped`` here;
    the bundle-level rollup surfaces it.

    Under ``EXAMLOPS_DB_BACKEND=postgres`` the platform DB is skipped **on purpose**: its state
    lives in Postgres and is dumped by :mod:`examlops.backup.postgres_tier`.
    """
    sqlite_dir = dest_dir / "sqlite"
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    items: list[dict[str, Any]] = []

    for spec in _SQLITE_DBS:
        if spec["name"] == "platform" and _platform_is_postgres():
            # Backing up the leftover file here would produce a bundle that looks complete and
            # restores nothing: with the Postgres engine selected, `platform.db` holds no state.
            items.append(
                {
                    "name": "platform",
                    "db_env": spec["env"],
                    "status": SKIPPED,
                    "reason": "EXAMLOPS_DB_BACKEND=postgres — platform state is in the postgres tier",
                }
            )
            continue
        src = _resolve_db_path(spec)
        if not src or not Path(src).exists():
            items.append(
                {
                    "name": spec["name"],
                    "db_env": spec["env"],
                    "status": SKIPPED,
                    "reason": f"{spec['env']} not present at {src or spec['default']}",
                }
            )
            continue
        dest = sqlite_dir / f"{spec['name']}-{ts}.db"
        manifest = _snapshot_one(src, dest)
        _write_manifest(dest, manifest)
        items.append(
            {
                "name": spec["name"],
                "db_env": spec["env"],
                "file": f"sqlite/{dest.name}",
                "sha256": manifest["sha256"],
                "size_bytes": manifest["size_bytes"],
                "table_counts": manifest["table_counts"],
                "audit_head_hash": manifest["audit_head_hash"],
                "status": OK,
            }
        )

    status = rollup_status([i["status"] for i in items])
    return TierResult("sqlite", status=status, items=items)


def restore_sqlite_tier(bundle_dir: Path, *, force: bool = False) -> list[dict[str, Any]]:
    """Restore each SQLite DB in a bundle back to its resolved live path (guarded + re-verified)."""
    manifest = _load_bundle_manifest(bundle_dir)
    results: list[dict[str, Any]] = []
    for item in manifest.get("tiers", {}).get("sqlite", {}).get("items", []):
        if item.get("status") != OK:
            continue
        spec = next((s for s in _SQLITE_DBS if s["name"] == item["name"]), None)
        if spec is None:
            continue
        src = bundle_dir / item["file"]
        dest = _resolve_db_path(spec)
        res = restore_backup(str(src), db_path=dest, force=force)
        results.append({"name": item["name"], **res})
    return results


def _load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    import json

    mp = bundle_dir / "bundle.manifest.json"
    if not mp.exists():
        raise ValueError(f"no bundle.manifest.json in {bundle_dir}")
    return json.loads(mp.read_text())
