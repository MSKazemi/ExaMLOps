"""Backup / restore + DR-drill (enterprise-readiness Phase 0, item 0.9).

Proves the platform-DB backup path is trustworthy end-to-end: an online snapshot is
transactionally consistent, its manifest verifies, a corrupted backup is rejected, and a
full create → wipe → restore round trip recovers every row AND leaves the tamper-evident
audit chain valid. This is the tested restore runbook the roadmap requires.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


# The SQLite backup tier snapshots ``platform.db`` through sqlite3's online-backup API, so these
# assertions only mean anything on the SQLite engine. Under ``EXAMLOPS_DB_BACKEND=postgres`` the
# rows live in Postgres and there is no file to snapshot — the pg_dump tier that will cover it is
# tracked in the Postgres migration plan, not silently assumed here.
sqlite_tier_only = pytest.mark.skipif(
    os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres",
    reason="SQLite backup tier: no platform.db file exists under the Postgres backend",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    # Keep the pre-op auto-backup (fired by `exa backup restore`) hermetic — off the repo cwd.
    monkeypatch.setenv("EXAMLOPS_BACKUP_DIR", str(tmp_path / "auto"))
    monkeypatch.setenv("MLFLOW_SQLITE_DB", str(tmp_path / "no-mlflow.db"))
    # The pre-op auto-backup runs the config tier, which tars the *operator's* ~/.config/examlops.
    # Left unset, this test copied a real clusters.yaml — hostname, ssh user, key path — into its
    # bundle, and its contents varied with whose machine it ran on.
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    (tmp_path / "cfg").mkdir(exist_ok=True)
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def _seed(pdb) -> None:
    for i in range(5):
        pdb.write_audit_event("test", "alice", f"act{i}", "JPCP")
    pdb.write_drift_snapshot("JPCP", "Production", 1.5, "job-1")
    pdb.set_traffic_rules("JPCP", {"Production": 90, "Canary": 10})


@sqlite_tier_only
def test_create_writes_backup_and_manifest(db, tmp_path):
    from examlops import backup

    _seed(db)
    manifest = backup.create_backup(str(tmp_path / "backups"))
    bfile = tmp_path / "backups" / manifest["backup_file"]
    assert bfile.exists()
    assert (bfile.parent / (bfile.name + ".manifest.json")).exists()
    assert manifest["table_counts"]["audit_events"] == 5
    assert manifest["audit_head_hash"]
    assert len(manifest["sha256"]) == 64


@sqlite_tier_only
def test_verify_passes_for_good_backup_and_fails_for_corrupted(db, tmp_path):
    from examlops import backup

    _seed(db)
    manifest = backup.create_backup(str(tmp_path / "backups"))
    bfile = tmp_path / "backups" / manifest["backup_file"]

    assert backup.verify_backup(str(bfile))["ok"] is True

    # Flip a byte in the middle → checksum mismatch → rejected.
    raw = bytearray(bfile.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    bfile.write_bytes(raw)
    bad = backup.verify_backup(str(bfile))
    assert bad["ok"] is False


@sqlite_tier_only
def test_dr_drill_create_wipe_restore_roundtrip(db, tmp_path, monkeypatch):
    """The core DR drill: snapshot, destroy the live DB, restore, assert full recovery."""
    from examlops import backup

    _seed(db)
    manifest = backup.create_backup(str(tmp_path / "backups"))
    bfile = str(tmp_path / "backups" / manifest["backup_file"])

    # Simulate catastrophic loss: delete the live DB (+ WAL sidecars).
    live = tmp_path / "platform.db"
    for p in (live, Path(str(live) + "-wal"), Path(str(live) + "-shm")):
        if p.exists():
            p.unlink()

    result = backup.restore_backup(bfile, force=True)
    assert result["verification"]["ok"] is True
    assert result["verification"]["integrity_check"] == "ok"
    assert result["verification"]["audit_chain_ok"] is True

    # Every row is back and the audit chain still verifies live.
    import examlops.platform_db as pdb

    assert pdb.verify_audit_chain()["ok"] is True
    with pdb.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert n == 5
    assert pdb.get_traffic_rules("JPCP") == {"Production": 90, "Canary": 10}


@sqlite_tier_only
def test_restore_refuses_to_clobber_nonempty_without_force(db, tmp_path):
    from examlops import backup

    _seed(db)
    manifest = backup.create_backup(str(tmp_path / "backups"))
    bfile = str(tmp_path / "backups" / manifest["backup_file"])
    # Target DB is non-empty (seeded) → restore without force must refuse.
    with pytest.raises(ValueError, match="non-empty"):
        backup.restore_backup(bfile, force=False)


@sqlite_tier_only
def test_list_backups_newest_first(db, tmp_path):
    from examlops import backup

    _seed(db)
    m1 = backup.create_backup(str(tmp_path / "backups"))
    rows = backup.list_backups(str(tmp_path / "backups"))
    assert rows and rows[0]["file"] == m1["backup_file"]
    assert rows[0]["has_manifest"] is True


@sqlite_tier_only
def test_cli_backup_create_verify_restore(db, tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _seed(db)
    runner = CliRunner()
    out_dir = str(tmp_path / "backups")

    r1 = runner.invoke(app, ["backup", "create", "--out", out_dir])
    assert r1.exit_code == 0, r1.output

    backup_files = list((tmp_path / "backups").glob("platform-*.db"))
    assert backup_files
    bfile = str(backup_files[0])

    r2 = runner.invoke(app, ["backup", "verify", bfile])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["backup", "restore", bfile, "--force", "--yes"])
    assert r3.exit_code == 0, r3.output
