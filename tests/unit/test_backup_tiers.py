"""Per-tier backup unit tests — postgres / objects / config — all with NO live stack.

Postgres and object tiers are exercised via their test seams (``_run``/``shutil.which`` and an
in-memory fake S3 client) so the graceful-degradation matrix (tool-missing, endpoint-down,
boto3-missing, happy-path, restore) is covered without Docker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


# ── postgres tier ──────────────────────────────────────────────────────────────


def test_postgres_skips_when_binary_missing(tmp_path, monkeypatch):
    from examlops.backup import postgres_tier
    from examlops.backup._manifest import TierUnavailable

    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: None)
    with pytest.raises(TierUnavailable, match="pg_dump not on PATH"):
        postgres_tier.backup_postgres_tier(tmp_path)


def test_postgres_skips_on_connection_refused(tmp_path, monkeypatch):
    from examlops.backup import postgres_tier
    from examlops.backup._manifest import TierUnavailable

    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")
    monkeypatch.setattr(
        postgres_tier,
        "_run",
        lambda cmd, env: (1, "could not connect to server: Connection refused"),
    )
    with pytest.raises(TierUnavailable, match="unreachable"):
        postgres_tier.backup_postgres_tier(tmp_path)


def test_postgres_happy_path_writes_dumps(tmp_path, monkeypatch):
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_BACKUP_PG_DBS", "mlflow,prefect")
    # Pin the engine: under Postgres the tier also dumps the platform datastore, which has its
    # own tests in test_backup_platform_datastore.py. This one is about the configured DB list.
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")

    def fake_run(cmd, env):
        # cmd = ["pg_dump","-Fc","-d",db,"-f",path]
        Path(cmd[-1]).write_bytes(b"PGDMP-fake-dump")
        return 0, ""

    monkeypatch.setattr(postgres_tier, "_run", fake_run)
    res = postgres_tier.backup_postgres_tier(tmp_path)
    assert res.status == "ok"
    names = {i["name"] for i in res.items}
    assert names == {"mlflow", "prefect"}
    assert all(i["sha256"] for i in res.items)


def test_postgres_restore_requires_force(tmp_path):
    from examlops.backup import postgres_tier

    with pytest.raises(ValueError, match="force=True"):
        postgres_tier.restore_postgres_tier(tmp_path, force=False)


# ── objects tier ───────────────────────────────────────────────────────────────


class _FakeS3:
    """Minimal in-memory S3 double: dict of {bucket: {key: bytes}}."""

    def __init__(self, data=None):
        self.data = data or {}

    def list_objects_v2(self, **kw):
        bucket = kw["Bucket"]
        if bucket not in self.data:
            raise RuntimeError(f"NoSuchBucket: {bucket}")
        contents = [
            {"Key": k, "Size": len(v), "ETag": f'"{len(v)}"'} for k, v in self.data[bucket].items()
        ]
        return {"Contents": contents, "IsTruncated": False}

    def download_file(self, bucket, key, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.data[bucket][key])

    def upload_file(self, src, bucket, key):
        self.data.setdefault(bucket, {})[key] = Path(src).read_bytes()


def test_objects_skips_when_boto3_missing(tmp_path, monkeypatch):
    from examlops.backup import objects_tier
    from examlops.backup._manifest import TierUnavailable

    def _raise():
        raise TierUnavailable("boto3 not installed — pip install 'examlops[backup]'")

    monkeypatch.setattr(objects_tier, "_s3_client", _raise)
    with pytest.raises(TierUnavailable, match="boto3"):
        objects_tier.backup_objects_tier(tmp_path)


def test_objects_skips_when_endpoint_down(tmp_path, monkeypatch):
    from examlops.backup import objects_tier
    from examlops.backup._manifest import TierUnavailable

    class _Down:
        def list_objects_v2(self, **kw):
            raise RuntimeError("Could not connect to the endpoint URL")

    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: _Down())
    with pytest.raises(TierUnavailable, match="cannot list bucket"):
        objects_tier.backup_objects_tier(tmp_path)


def test_objects_mirror_and_restore_roundtrip(tmp_path, monkeypatch):
    from examlops.backup import objects_tier

    fake = _FakeS3({"mlflow-artifacts": {"a.txt": b"hello", "sub/b.bin": b"\x00\x01\x02"}})
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: fake)

    res = objects_tier.backup_objects_tier(tmp_path)
    assert res.status == "ok"
    item = res.items[0]
    assert item["object_count"] == 2
    assert item["sha256"]  # tree hash over the sorted index
    # Index + files exist on disk.
    assert (tmp_path / "objects" / "mlflow-artifacts" / "_index.json").exists()
    assert (tmp_path / "objects" / "mlflow-artifacts" / "a.txt").read_bytes() == b"hello"

    # Restore into an empty target bucket.
    import json

    (tmp_path / "bundle.manifest.json").write_text(
        json.dumps({"tiers": {"objects": {"items": res.items}}})
    )
    fake.data["mlflow-artifacts"] = {}  # empty → restore allowed without force
    out = objects_tier.restore_objects_tier(tmp_path, force=False)
    assert out[0]["uploaded"] == 2
    assert fake.data["mlflow-artifacts"]["a.txt"] == b"hello"


# ── config tier ────────────────────────────────────────────────────────────────


def test_config_tier_tars_and_records_key_ids(tmp_path, monkeypatch):
    from examlops.backup import config_tier

    cfg = tmp_path / "cfg" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[urls]\n")
    (cfg.parent / "clusters.yaml").write_text("clusters: []\n")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEYS", "k1:xxxx,k2:yyyy")
    # The legacy dashboard KEK is a *second* source of key ids; unset it so this test measures the
    # keyring alone. A shell that sourced the repo's .env exports it and the assertion below then
    # fails on a `legacy-dashboard` entry that has nothing to do with the code under test.
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)

    res = config_tier.backup_config_tier(tmp_path)
    assert res.status == "ok"
    item = res.items[0]
    assert item["kek_present_in_bundle"] is False
    assert item["secrets_key_ids"] == ["k1", "k2"]
    assert item["warnings"]  # KEK-out-of-band warning present
    assert (tmp_path / "config" / "config-tree.tar.gz").exists()


def test_config_tier_skips_when_dir_absent(tmp_path, monkeypatch):
    from examlops.backup import config_tier

    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "does-not-exist" / "config.toml"))
    # The site configuration directory (policy/providers/HPC registry) is captured too when it
    # lives elsewhere (ADR 0128) — point it at nothing, or the host's ~/.config/examlops counts.
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "no-site-config"))
    monkeypatch.delenv("EXAMLOPS_SECRETS_KEYS", raising=False)
    res = config_tier.backup_config_tier(tmp_path)
    assert res.status == "skipped"
