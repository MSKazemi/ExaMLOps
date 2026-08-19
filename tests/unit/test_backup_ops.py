"""Backup operational layer — retention, scheduler, auto-hook, off-site remote, and CLI.

Proves rotation keeps the right bundles (and never the last good one), the scheduler survives a
failing cycle, the pre-op auto-hook never raises, off-site push/list/pull round-trips against a
fake S3, and the extended CLI (create --all, verify-bundle, restore-bundle, status) plus the
legacy 4 commands all behave.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


# See the note in ``test_backup_restore.py``: the SQLite tier has no file to snapshot under the
# Postgres backend.
sqlite_tier_only = pytest.mark.skipif(
    os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres",
    reason="SQLite backup tier: no platform.db file exists under the Postgres backend",
)


def _make_bundle(dir_path: Path, bundle_id: str, created: str, status: str = "ok") -> Path:
    bd = dir_path / bundle_id
    bd.mkdir(parents=True)
    (bd / "bundle.manifest.json").write_text(
        json.dumps({"bundle_id": bundle_id, "created_at": created, "overall_status": status})
    )
    return bd


# ── retention ──────────────────────────────────────────────────────────────────


def test_retention_keep_n(tmp_path):
    from examlops.backup import retention

    for i in range(5):
        _make_bundle(
            tmp_path, f"examlops-backup-2026072{i}T000000Z", f"2026-07-2{i}T00:00:00+00:00"
        )
    pruned = retention.prune(str(tmp_path), keep_n=2)
    assert len(pruned) == 3
    remaining = sorted(p.name for p in tmp_path.glob("examlops-backup-*"))
    assert remaining == [
        "examlops-backup-20260723T000000Z",
        "examlops-backup-20260724T000000Z",
    ]


def test_retention_never_prunes_newest(tmp_path):
    from examlops.backup import retention

    _make_bundle(tmp_path, "examlops-backup-20260720T000000Z", "2026-07-20T00:00:00+00:00")
    pruned = retention.prune(str(tmp_path), keep_n=0)
    assert pruned == []  # the sole/newest bundle is protected
    assert list(tmp_path.glob("examlops-backup-*"))


def test_retention_keeps_last_good_bundle(tmp_path):
    from examlops.backup import retention

    _make_bundle(tmp_path, "examlops-backup-20260720T000000Z", "2026-07-20T00:00:00+00:00", "ok")
    _make_bundle(
        tmp_path, "examlops-backup-20260721T000000Z", "2026-07-21T00:00:00+00:00", "failed"
    )
    # keep_n=1 would drop the older 'ok' one, leaving only 'failed' — safety must keep the good one.
    retention.prune(str(tmp_path), keep_n=1)
    names = {p.name for p in tmp_path.glob("examlops-backup-*")}
    assert "examlops-backup-20260720T000000Z" in names  # last good survives


# ── scheduler ──────────────────────────────────────────────────────────────────


def test_scheduler_once_produces_bundle(tmp_path, monkeypatch):
    from examlops.backup import schedule

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "nope2.db"))
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "nope3.db"))
    monkeypatch.setenv("MLFLOW_SQLITE_DB", str(tmp_path / "nope4.db"))
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[urls]\n")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg))
    import examlops.platform_db as pdb

    pdb.init_db()

    res = schedule.run_scheduler(
        out_dir=str(tmp_path / "bk"), tiers=["sqlite", "config"], push=False, once=True
    )
    assert res is not None
    assert (tmp_path / "bk" / res.bundle_id / "bundle.manifest.json").exists()


def test_scheduler_survives_failing_cycle(tmp_path, monkeypatch):
    from examlops.backup import bundle, schedule

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bundle, "create_bundle", boom)
    # Should not raise despite the failing cycle.
    res = schedule.run_scheduler(
        out_dir=str(tmp_path / "bk"), tiers=["sqlite"], push=False, once=True
    )
    assert res is None


# ── auto-hook ──────────────────────────────────────────────────────────────────


def test_auto_backup_before_returns_dir(tmp_path, monkeypatch):
    from examlops import backup

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "n1.db"))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "n2.db"))
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "n3.db"))
    monkeypatch.setenv("MLFLOW_SQLITE_DB", str(tmp_path / "n4.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    import examlops.platform_db as pdb

    pdb.init_db()
    out = backup.auto_backup_before("unit-test-op")
    assert out is not None
    assert Path(out).exists()


def test_auto_backup_never_raises_on_failure(monkeypatch):
    from examlops import backup
    from examlops.backup import bundle

    monkeypatch.setattr(
        bundle, "create_bundle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert backup.auto_backup_before("op") is None  # swallowed, never raises


# ── off-site remote ────────────────────────────────────────────────────────────


class _FakeS3:
    def __init__(self):
        self.data: dict[str, dict[str, bytes]] = {}

    def upload_file(self, src, bucket, key):
        self.data.setdefault(bucket, {})[key] = Path(src).read_bytes()

    def download_file(self, bucket, key, dest):
        Path(dest).write_bytes(self.data[bucket][key])

    def list_objects_v2(self, **kw):
        bucket, prefix = kw["Bucket"], kw.get("Prefix", "")
        contents = [{"Key": k} for k in self.data.get(bucket, {}) if k.startswith(prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 kwarg name
        import io

        return {"Body": io.BytesIO(self.data[Bucket][Key])}


def test_remote_push_list_pull_roundtrip(tmp_path, monkeypatch):
    from examlops.backup import remote

    fake = _FakeS3()
    monkeypatch.setattr(remote, "_s3_client", lambda: fake)
    monkeypatch.setenv("EXAMLOPS_BACKUP_S3_URI", "s3://examlops-backups/nightly")

    # Build a minimal bundle dir.
    bd = tmp_path / "examlops-backup-20260722T000000Z"
    bd.mkdir()
    (bd / "bundle.manifest.json").write_text(
        json.dumps(
            {
                "bundle_id": "examlops-backup-20260722T000000Z",
                "created_at": "2026-07-22T00:00:00+00:00",
                "overall_status": "ok",
            }
        )
    )
    (bd / "payload.txt").write_text("data")

    pushed = remote.push(str(bd))
    assert pushed["pushed"] == bd.name

    listed = remote.list_remote()
    assert listed and listed[0]["bundle_id"] == bd.name

    extracted = remote.pull(bd.name, str(tmp_path / "restored"))
    assert (Path(extracted) / "payload.txt").read_text() == "data"


# ── CLI ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "n1.db"))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "n2.db"))
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "n3.db"))
    monkeypatch.setenv("MLFLOW_SQLITE_DB", str(tmp_path / "n4.db"))
    # Restore commands take a pre-op auto-backup — keep it in tmp, not the repo cwd.
    monkeypatch.setenv("EXAMLOPS_BACKUP_DIR", str(tmp_path / "auto"))
    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[urls]\n")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg))
    import examlops.platform_db as pdb

    pdb.init_db()
    pdb.write_audit_event("t", "a", "x", "J")
    return tmp_path


@sqlite_tier_only
def test_cli_legacy_four_still_work(cli_env):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    out = str(cli_env / "bk")
    assert runner.invoke(app, ["backup", "create", "--out", out]).exit_code == 0
    bfile = str(next((cli_env / "bk").glob("platform-*.db")))
    assert runner.invoke(app, ["backup", "verify", bfile]).exit_code == 0
    assert runner.invoke(app, ["backup", "restore", bfile, "--force", "--yes"]).exit_code == 0
    assert runner.invoke(app, ["backup", "list", "--dir", out]).exit_code == 0


@sqlite_tier_only
def test_cli_bundle_create_verify_restore_status(cli_env):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    out = str(cli_env / "bk")

    r = runner.invoke(app, ["backup", "create", "--all", "--out", out])
    assert r.exit_code == 0, r.output
    bundle_dir = str(next((cli_env / "bk").glob("examlops-backup-*")))

    assert runner.invoke(app, ["backup", "verify-bundle", bundle_dir]).exit_code == 0

    # restore just the sqlite tier
    (cli_env / "platform.db").unlink()
    r2 = runner.invoke(
        app, ["backup", "restore-bundle", bundle_dir, "--tier", "sqlite", "--force", "--yes"]
    )
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["--json", "backup", "status", "--dir", out])
    assert r3.exit_code == 0
    assert "latest_bundle" in r3.output


def test_cli_schedule_once(cli_env):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    out = str(cli_env / "bk")
    r = runner.invoke(
        app, ["backup", "schedule", "--once", "--tiers", "sqlite,config", "--out", out]
    )
    assert r.exit_code == 0, r.output
    assert list((cli_env / "bk").glob("examlops-backup-*"))
