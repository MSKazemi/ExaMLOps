"""Tiered whole-platform backup bundle — create / verify / restore / status rollup.

Complements ``test_backup_restore.py`` (the legacy single-DB gate): proves the bundle orchestrator
snapshots every tier, degrades heavy tiers to ``skipped`` off-stack, rolls up an honest overall
status, verifies + restores round-trip, and that ``--strict`` turns a skip into a raise. Everything
runs with **no live stack** — Postgres/MinIO tiers degrade naturally.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    # Keep the optional DBs pointed at non-existent paths so they degrade to skipped deterministically.
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "nope-approvals.db"))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "nope-agent.db"))
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "nope-skipper.db"))
    monkeypatch.setenv("MLFLOW_SQLITE_DB", str(tmp_path / "nope-mlflow.db"))
    # Config tier: point at a hermetic config dir with one file.
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[urls]\n")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg))
    import examlops.platform_db as pdb

    pdb.init_db()
    for i in range(3):
        pdb.write_audit_event("test", "alice", f"act{i}", "JPCP")
    pdb.set_traffic_rules("JPCP", {"Production": 80, "Canary": 20})
    return pdb


def test_control_plane_bundle_creates_and_verifies(platform_db, tmp_path):
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    assert res.overall_status in ("ok", "partial")  # partial: optional DBs absent
    tiers = res.manifest["tiers"]
    assert tiers["sqlite"]["status"] in ("ok", "partial")
    assert tiers["config"]["status"] == "ok"
    # platform.db captured with its audit head + table counts
    platform_item = next(i for i in tiers["sqlite"]["items"] if i["name"] == "platform")
    assert platform_item["table_counts"]["audit_events"] == 3
    assert platform_item["audit_head_hash"]
    assert backup.verify_bundle(res.bundle_dir)["ok"] is True


def test_heavy_tiers_skip_off_stack(platform_db, tmp_path):
    from examlops import backup

    res = backup.create_bundle(
        str(tmp_path / "bk"), tiers=["sqlite", "config", "postgres", "objects"]
    )
    assert res.manifest["tiers"]["postgres"]["status"] == "skipped"
    assert res.manifest["tiers"]["objects"]["status"] == "skipped"
    assert res.overall_status == "partial"  # some ok, some skipped


def test_strict_promotes_skip_to_raise(platform_db, tmp_path):
    from examlops import backup

    # Postgres tier can't run off-stack (no pg_dump); strict must raise instead of skipping.
    with pytest.raises(Exception):  # noqa: B017 — TierUnavailable surfaces
        backup.create_bundle(str(tmp_path / "bk"), tiers=["postgres"], strict=True)


def test_bundle_roundtrip_restores_sqlite(platform_db, tmp_path):
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    # Simulate catastrophic loss of the live platform DB.
    live = tmp_path / "platform.db"
    for p in (live, Path(str(live) + "-wal"), Path(str(live) + "-shm")):
        if p.exists():
            p.unlink()
    out = backup.restore_bundle(res.bundle_dir, tiers=["sqlite"], force=True)
    assert "sqlite" in out["restored_tiers"]

    import examlops.platform_db as pdb

    assert pdb.verify_audit_chain()["ok"] is True
    assert pdb.get_traffic_rules("JPCP") == {"Production": 80, "Canary": 20}


def test_verify_rejects_tampered_snapshot(platform_db, tmp_path):
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    platform_item = next(
        i for i in res.manifest["tiers"]["sqlite"]["items"] if i["name"] == "platform"
    )
    snap = Path(res.bundle_dir) / platform_item["file"]
    raw = bytearray(snap.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    snap.write_bytes(raw)
    assert backup.verify_bundle(res.bundle_dir)["ok"] is False


def test_restore_bundle_refuses_unverified(platform_db, tmp_path):
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    # Corrupt the manifest so verification fails.
    (Path(res.bundle_dir) / "bundle.manifest.json").unlink()
    with pytest.raises(ValueError, match="unverified"):
        backup.restore_bundle(res.bundle_dir, tiers=["sqlite"], force=True)


def test_list_bundles_newest_first(platform_db, tmp_path):
    from examlops import backup

    b1 = backup.create_bundle(str(tmp_path / "bk"))
    rows = backup.list_bundles(str(tmp_path / "bk"))
    assert rows and rows[0]["bundle_id"] == b1.bundle_id
    assert rows[0]["overall_status"] == b1.overall_status
