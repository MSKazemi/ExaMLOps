"""Consistent backup / restore for the platform SQLite datastore (Phase 0 item 0.9).

The ``platform.db`` monolith is the data layer, event bus, and security store for 5+ processes
(see the enterprise-readiness audit). Losing it — or restoring it inconsistently — is the single
biggest blast-radius event. This module provides a **tested** backup/restore path with:

  * an **online, hot backup** via SQLite's native ``Connection.backup()`` API — a
    transactionally-consistent snapshot taken while writers are active (a plain file ``cp`` of a
    live WAL database can capture a torn state and is NOT safe);
  * a **manifest** (sha256 of the snapshot, table row counts, audit-chain head hash, timestamp)
    written alongside each backup so a restore can be *verified before it is trusted*;
  * a **guarded restore** that refuses to clobber a non-empty database unless forced, restores via
    the online API, then re-verifies integrity + the audit hash chain — the DR-drill round trip.

Postgres and MinIO backups (``pg_dump`` / ``mc mirror``) are out of process scope here — they need
their own external tools — and are documented in ``docs/guides/backup-restore.md``; this module owns
the SQLite tier, which is what the ``SqliteBackend`` (item 0.1) still runs on by default.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from examlops.resilience import db as _rdb

_MANIFEST_SUFFIX = ".manifest.json"


def _default_db_path() -> str:
    from examlops.platform_db import _db_path

    return _db_path()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


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


def create_backup(out_dir: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Take an online snapshot of the platform DB into ``out_dir`` + write its manifest.

    Returns the manifest dict (also persisted to ``<backup>.manifest.json``).
    """
    src = db_path or _default_db_path()
    if not Path(src).exists():
        raise FileNotFoundError(f"platform DB not found at {src}")
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(out_dir) / f"platform-{ts}.db"
    _online_backup(src, dest)

    snap = _rdb.connect(str(dest))
    try:
        manifest: dict[str, Any] = {
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
    (dest.parent / (dest.name + _MANIFEST_SUFFIX)).write_text(json.dumps(manifest, indent=2))
    return manifest


def _read_manifest(backup_path: Path) -> dict[str, Any] | None:
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
