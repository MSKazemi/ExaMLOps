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

    def head_bucket(self, Bucket):  # noqa: N803 — boto3's parameter name
        if Bucket not in self.data:
            raise RuntimeError(f"NoSuchBucket: {Bucket}")

    def create_bucket(self, Bucket):  # noqa: N803 — boto3's parameter name
        self.data.setdefault(Bucket, {})

    def upload_file(self, src, bucket, key):
        # Was `self.data.setdefault(bucket, {})`, which quietly created the bucket — so a restore
        # into an object store that had lost its buckets (the *whole point* of this tier) passed
        # here and failed against a real MinIO with `NoSuchBucket`. A double that is more
        # forgiving than the thing it stands in for tests nothing on the path that matters.
        if bucket not in self.data:
            raise RuntimeError(f"NoSuchBucket: {bucket}")
        self.data[bucket][key] = Path(src).read_bytes()


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


def test_objects_skips_a_missing_bucket_but_still_backs_up_the_rest(tmp_path, monkeypatch):
    """A default-bucket-list change (ADR 0130's EXAMLOPS_DATA_BUCKET) must not break an existing
    install's objects backup before minio-init has had a chance to create the new bucket."""
    from examlops.backup import objects_tier
    from examlops.backup._manifest import OK, PARTIAL, SKIPPED

    # `mlflow-artifacts` exists; `examlops-data` does not — _FakeS3 raises NoSuchBucket for it.
    fake = _FakeS3({"mlflow-artifacts": {"a.txt": b"hello"}})
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts,examlops-data")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: fake)

    res = objects_tier.backup_objects_tier(tmp_path)
    assert res.status == PARTIAL, res  # some ok, some skipped — never a silent full success
    by_bucket = {i["bucket"]: i for i in res.items}
    assert by_bucket["mlflow-artifacts"]["status"] == OK
    assert by_bucket["mlflow-artifacts"]["object_count"] == 1
    assert by_bucket["examlops-data"]["status"] == SKIPPED
    assert "does not exist" in by_bucket["examlops-data"]["reason"]
    # Nothing raised — the missing bucket is visible in the manifest, not hidden by an exception.


def test_objects_skips_directory_marker_keys(tmp_path, monkeypatch):
    """pyarrow's S3 filesystem (the dataplane's) writes zero-byte `prefix/` marker objects; the
    mirror must skip them — written as files they would block the real keys under the prefix."""
    from examlops.backup import objects_tier
    from examlops.backup._manifest import OK

    fake = _FakeS3(
        {"examlops-data": {"dataplane/": b"", "dataplane/_global/s/_latest": b"x", "a.txt": b"hi"}}
    )
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "examlops-data")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: fake)

    res = objects_tier.backup_objects_tier(tmp_path)
    assert res.status == OK, res
    assert res.items[0]["object_count"] == 2
    assert (
        tmp_path / "objects" / "examlops-data" / "dataplane" / "_global" / "s" / "_latest"
    ).is_file()


def test_objects_a_real_list_error_still_fails_the_whole_tier(tmp_path, monkeypatch):
    """Only "bucket does not exist" degrades; every other list failure still aborts the tier."""
    from examlops.backup import objects_tier
    from examlops.backup._manifest import TierUnavailable

    class _Down:
        def list_objects_v2(self, **kw):
            raise RuntimeError("Could not connect to the endpoint URL")

    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts,examlops-data")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: _Down())
    with pytest.raises(TierUnavailable, match="cannot list bucket"):
        objects_tier.backup_objects_tier(tmp_path)


def test_bucket_missing_detects_a_real_botocore_client_error():
    """The boto3 shape (`exc.response`), not just the test double's plain string."""
    from examlops.backup.objects_tier import _bucket_missing

    class _ClientError(Exception):
        def __init__(self, code, status):
            self.response = {
                "Error": {"Code": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    assert _bucket_missing(_ClientError("NoSuchBucket", 404))
    assert _bucket_missing(_ClientError("SomethingElse", 404))
    assert not _bucket_missing(_ClientError("AccessDenied", 403))
    assert not _bucket_missing(RuntimeError("Could not connect to the endpoint URL"))
    assert _bucket_missing(RuntimeError("NoSuchBucket: examlops-data"))


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
    assert out[0]["uploaded"] == 2 and out[0]["ok"] is True
    assert out[0]["bucket_created"] is False  # it was still there
    assert fake.data["mlflow-artifacts"]["a.txt"] == b"hello"


def test_restore_recreates_a_bucket_the_disaster_took(tmp_path, monkeypatch):
    """The case this tier exists for: the object store is gone, buckets and all.

    `docs/guides/backup-restore.md` puts objects **first** in its recovery order, because model
    artifacts have to exist before the registry's references to them resolve. Restoring into a
    store that had lost the bucket raised a raw boto3 `NoSuchBucket`, so the first step of a
    documented full-disaster recovery ended in a stack trace. Verified against a real MinIO before
    and after the fix; the in-memory double had hidden it by creating buckets on upload.
    """
    import json

    from examlops.backup import objects_tier

    fake = _FakeS3({"mlflow-artifacts": {"a.txt": b"hello", "sub/b.bin": b"\x00\x01\x02"}})
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: fake)

    res = objects_tier.backup_objects_tier(tmp_path)
    (tmp_path / "bundle.manifest.json").write_text(
        json.dumps({"tiers": {"objects": {"items": res.items}}})
    )

    del fake.data["mlflow-artifacts"]  # the store lost the bucket itself
    out = objects_tier.restore_objects_tier(tmp_path, force=True)

    assert out[0]["ok"] is True and out[0]["bucket_created"] is True
    assert out[0]["uploaded"] == 2
    assert fake.data["mlflow-artifacts"]["a.txt"] == b"hello"
    assert fake.data["mlflow-artifacts"]["sub/b.bin"] == b"\x00\x01\x02"


def test_a_bucket_that_fails_to_restore_is_reported_not_raised(tmp_path, monkeypatch):
    """One bad bucket must not cost the report for every other bucket.

    An object store is restored bucket by bucket; the first failure ending the tier is how a
    partial restore gets mistaken for a total one. The per-bucket `ok` is also what
    `restore_bundle` aggregates — without it, an objects restore could not be reported as having
    failed at all, whatever happened to it.
    """
    import json

    from examlops.backup import bundle as bundle_mod
    from examlops.backup import objects_tier

    fake = _FakeS3({"mlflow-artifacts": {"a.txt": b"hello"}})
    monkeypatch.setenv("EXAMLOPS_BACKUP_BUCKETS", "mlflow-artifacts")
    monkeypatch.setattr(objects_tier, "_s3_client", lambda: fake)
    res = objects_tier.backup_objects_tier(tmp_path)
    (tmp_path / "bundle.manifest.json").write_text(
        json.dumps(
            {
                "tiers": {"objects": {"items": res.items}},
                "data_format": 1,
                "min_reader_format": 1,
            }
        )
    )

    def _no(src, bucket, key):
        raise RuntimeError("disk full on the object store")

    monkeypatch.setattr(fake, "upload_file", _no)
    out = objects_tier.restore_objects_tier(tmp_path, force=True)
    assert out[0]["ok"] is False and out[0]["uploaded"] == 0 and out[0]["expected"] == 1
    assert "disk full" in out[0]["reason"]

    # And the whole-bundle caller must see it — that is what `exa backup restore-bundle` exits on.
    result = bundle_mod.restore_bundle(str(tmp_path), tiers=["objects"], force=True)
    assert result["ok"] is False, result
    assert result["failed"] and result["failed"][0]["bucket"] == "mlflow-artifacts"


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


# ─── the approvals tier follows the control plane (plan P0.9 / finding B6) ───────────────────
#
# The compose sidecar hard-coded CONTROL_PLANE_DB=/data/approvals.db long after the control plane
# moved its state into the shared platform.db. Every bundle therefore carried an "approvals" item
# that was a stale file nothing wrote any more, beside the real state it did not know was there.


def _sqlite_items(tmp_path):
    from examlops.backup import sqlite_tier

    return {i["name"]: i for i in sqlite_tier.backup_sqlite_tier(tmp_path / "bundle").items}


def _seed(path):
    from examlops.resilience import db as _rdb

    conn = _rdb.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS pending_approvals (id TEXT)")
    conn.commit()
    conn.close()


def test_approvals_inside_the_platform_db_is_not_snapshotted_twice(tmp_path, monkeypatch):
    shared = tmp_path / "platform.db"
    _seed(shared)
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("PLATFORM_DB", str(shared))
    monkeypatch.delenv("CONTROL_PLANE_DB", raising=False)

    items = _sqlite_items(tmp_path)

    assert items["platform"]["status"] == "ok"
    assert items["approvals"]["status"] == "skipped"
    assert "inside the platform DB" in items["approvals"]["reason"]


def test_a_separate_control_plane_db_is_still_backed_up(tmp_path, monkeypatch):
    shared, own = tmp_path / "platform.db", tmp_path / "cp.db"
    _seed(shared)
    _seed(own)
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("PLATFORM_DB", str(shared))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(own))

    assert _sqlite_items(tmp_path)["approvals"]["status"] == "ok"


def test_control_plane_state_on_postgres_is_left_to_the_postgres_tier(tmp_path, monkeypatch):
    leftover = tmp_path / "platform.db"
    _seed(leftover)
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("PLATFORM_DB", str(leftover))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(leftover))

    items = _sqlite_items(tmp_path)

    assert items["approvals"]["status"] == "skipped"
    assert "postgres tier" in items["approvals"]["reason"]


def test_compose_sidecar_follows_the_control_plane():
    import yaml

    compose = (
        Path(__file__).resolve().parents[2] / "platform/infra/docker-compose/docker-compose.yml"
    )
    env = yaml.safe_load(compose.read_text())["services"]["backup"]["environment"]
    assert env["CONTROL_PLANE_DB"] == "${CONTROL_PLANE_DB:-/state/platform.db}"
    assert "EXAMLOPS_DB_BACKEND" in env and "EXAMLOPS_POSTGRES_DSN" in env
