"""Tiered whole-platform backup bundle — create / verify / restore / status rollup.

Complements ``test_backup_restore.py`` (the legacy single-DB gate): proves the bundle orchestrator
snapshots every tier, degrades heavy tiers to ``skipped`` off-stack, rolls up an honest overall
status, verifies + restores round-trip, and that ``--strict`` turns a skip into a raise. Everything
runs with **no live stack** — Postgres/MinIO tiers degrade naturally.
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


@sqlite_tier_only
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


def test_heavy_tiers_skip_off_stack(platform_db, tmp_path, monkeypatch):
    """Off-stack is *established*, not assumed.

    This asserted that the object tier skips while doing nothing to make it skip — it passed only
    because nothing was answering on the default endpoint. That default used to be a port this
    project never serves, so the test could not fail; now that it points at the port the host
    actually publishes, a developer with `make stack-up` running would exercise the success branch
    and this would go red. Port 1 serves nothing, anywhere.
    """
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://127.0.0.1:1")
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


@sqlite_tier_only
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


# ── a partial restore must not present as a restore ───────────────────────────


def _bundle_with_an_extra_tier(bundle_dir: str, tier: str = "objects") -> None:
    """Add a tier to the bundle's manifest, as a `--with-objects` bundle would carry.

    The tier carries no items, so `verify_bundle` — which checks each item's checksum — still
    passes. The point of the test is a **verified** bundle whose heavy tier the default leaves
    behind, not a broken one.
    """
    import json

    from examlops.backup.bundle import _MANIFEST_NAME

    path = Path(bundle_dir) / _MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["tiers"][tier] = {
        "status": "ok",
        # One captured item: a tier with none holds nothing to restore, which is
        # exactly what `postgres`/`objects` look like when the stack was down.
        "items": [{"name": "models", "status": "ok", "bucket": "models"}],
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


@sqlite_tier_only
def test_a_restore_reports_the_tiers_it_did_not_restore(platform_db, tmp_path):
    """The default restores sqlite + config. A bundle holding more must say so.

    After a disaster the operator reads one line. `Restored tiers ['sqlite','config']` with a green
    tick, from a bundle that also held `objects` — the models and MLflow artifacts — reads as "the
    platform is back". The prompt names the tiers, but a scripted recovery passes `--yes` and never
    sees it. This module already argues the same point for failed items: "a restore that failed and
    reported success is worse than one that raised".
    """
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    _bundle_with_an_extra_tier(res.bundle_dir, "objects")

    out = backup.restore_bundle(res.bundle_dir, force=True)
    assert "objects" not in out["restored_tiers"], "the default must not restore heavy tiers"
    assert out.get("available_tiers"), "the result does not say what the bundle held"
    assert "objects" in out["available_tiers"]
    assert out.get("skipped_tiers") == ["objects"], (
        f"the restore did not report what it left behind: {out.get('skipped_tiers')!r}"
    )


@sqlite_tier_only
def test_a_full_restore_reports_nothing_skipped(platform_db, tmp_path):
    """Anti-vacuity: the field must reflect the restore, not always be populated."""
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    # A plain bundle taken with no stack up: `postgres`/`objects` are in the manifest with zero
    # captured items, so the default restore leaves *nothing* behind and must say nothing.
    out = backup.restore_bundle(res.bundle_dir, force=True)
    assert out["skipped_tiers"] == [], (
        "a tier the bundle captured nothing for was reported as left behind; a warning that fires "
        f"on every restore is one nobody reads: {out['skipped_tiers']}"
    )


@sqlite_tier_only
def test_the_operator_is_told_which_tiers_were_left_behind(platform_db, tmp_path):
    """Executed through the CLI: the library knowing is not the operator being told."""
    from typer.testing import CliRunner

    from examlops import backup
    from examlops.cli.main import app

    res = backup.create_bundle(str(tmp_path / "bk"))
    _bundle_with_an_extra_tier(res.bundle_dir, "objects")

    result = CliRunner().invoke(
        app, ["backup", "restore-bundle", res.bundle_dir, "--force", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "objects" in result.output, (
        "the operator was shown a green restore with no mention of the tier still missing:\n"
        f"{result.output}"
    )


@sqlite_tier_only
def test_the_result_says_whether_the_restore_was_complete(platform_db, tmp_path):
    """`ok` and `complete` answer different questions, and a DR script needs the second.

    `ok` means "everything I was asked to restore came back" — true for a deliberate
    `--tier sqlite` restore, and it must stay true or every partial restore would look like a
    failure. `complete` means "everything the bundle held came back", which is the question a
    disaster-recovery script is actually asking.
    """
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    _bundle_with_an_extra_tier(res.bundle_dir, "objects")

    partial = backup.restore_bundle(res.bundle_dir, force=True)
    assert partial["ok"] is True, "the tiers it was asked for did come back"
    assert partial["complete"] is False, (
        "a restore that left a captured tier behind reported itself as complete"
    )


@sqlite_tier_only
def test_a_restore_that_left_nothing_behind_is_complete(platform_db, tmp_path):
    """Anti-vacuity: `complete` must be able to be true."""
    from examlops import backup

    res = backup.create_bundle(str(tmp_path / "bk"))
    out = backup.restore_bundle(res.bundle_dir, force=True)
    assert out["skipped_tiers"] == []
    assert out["complete"] is True


@sqlite_tier_only
def test_a_scripted_restore_is_not_told_it_succeeded_fully(platform_db, tmp_path):
    """The `--json` path is what the DR runbook prescribes, and it returned exit 0 in silence.

    The warning is written to **stderr**, so stdout still carries exactly one JSON document — the
    structured-output contract — while a human watching a scripted recovery still sees it.
    """
    import json

    from typer.testing import CliRunner

    from examlops import backup
    from examlops.cli.main import app

    res = backup.create_bundle(str(tmp_path / "bk"))
    _bundle_with_an_extra_tier(res.bundle_dir, "objects")

    result = CliRunner().invoke(
        app, ["--json", "backup", "restore-bundle", res.bundle_dir, "--force", "--yes"]
    )
    payload = json.loads(result.stdout)
    assert payload["skipped_tiers"] == ["objects"]
    assert payload["complete"] is False, "the machine-readable answer still claimed a full restore"
    # stdout is still exactly one document — the warning must not have been printed there.
    assert result.stdout.strip().startswith("{") and result.stdout.strip().endswith("}")
    # …and it must have been printed *somewhere*: a human watching a scripted recovery is the
    # second reader of this command, and silence for them was the original defect.
    assert "NOT restored" in result.stderr, (
        f"the scripted path emitted no warning at all; stderr was {result.stderr!r}"
    )
