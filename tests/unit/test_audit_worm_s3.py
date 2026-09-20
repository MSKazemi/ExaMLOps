"""S3 Object-Lock WORM anchor (ADR 0028 decision 2), tested against a fake S3 client.

The fake mimics the parts of S3 the anchor relies on: ``put_object`` honours ``If-None-Match: *``
(a 412 on an existing key), records the Object Lock arguments, and rejects a lock request when the
bucket was not created with Object Lock enabled.
"""

from __future__ import annotations

import io
import json

import pytest
from typer.testing import CliRunner

from examlops import audit_worm
from examlops.cli.main import app

runner = CliRunner()


class FakeClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, *, lock_enabled: bool = True, down: bool = False):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[dict] = []
        self.lock_enabled = lock_enabled
        self.down = down

    def put_object(self, **kw):
        if self.down:
            raise FakeClientError("ServiceUnavailable")
        if "ObjectLockMode" in kw and not self.lock_enabled:
            raise FakeClientError("InvalidRequest")
        key = (kw["Bucket"], kw["Key"])
        if kw.get("IfNoneMatch") == "*" and key in self.objects:
            raise FakeClientError("PreconditionFailed")
        self.objects[key] = kw["Body"]
        self.puts.append(kw)

    def list_objects_v2(self, **kw):
        if self.down:
            raise FakeClientError("ServiceUnavailable")
        prefix = kw.get("Prefix", "")
        keys = sorted(k for (b, k) in self.objects if b == kw["Bucket"] and k.startswith(prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, **kw):
        return {"Body": io.BytesIO(self.objects[(kw["Bucket"], kw["Key"])])}


@pytest.fixture
def s3(tmp_path, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(audit_worm, "_s3_client", lambda: fake)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_PATH", "s3://audit-anchor/prod/chain")
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_FALLBACK_PATH", str(tmp_path / "fallback.jsonl"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    audit_worm.reset_anchor_failures()
    from examlops.platform_db import init_db

    init_db()
    return fake


def _cp(i: int, h: str = "h") -> dict:
    return {"head_id": i, "head_hash": f"{h}{i}", "key_id": "t"}


def test_anchor_writes_locked_objects_with_mode_and_retain_until(s3, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_S3_MODE", "COMPLIANCE")
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_S3_RETAIN_DAYS", "400")
    res = audit_worm.anchor_checkpoint_ex(_cp(1), ts="2026-09-20T00:00:00")
    assert res["backend"] == "s3" and res["degraded"] is False
    put = s3.puts[0]
    assert put["Bucket"] == "audit-anchor" and put["Key"] == "prod/chain/000000000001.json"
    assert put["ObjectLockMode"] == "COMPLIANCE"
    assert put["IfNoneMatch"] == "*"
    from datetime import UTC, datetime

    days = (put["ObjectLockRetainUntilDate"] - datetime.now(UTC)).days
    assert 398 <= days <= 400


def test_entries_are_chained_and_sequential(s3):
    h1 = audit_worm.anchor_checkpoint(_cp(1), ts="t1")
    h2 = audit_worm.anchor_checkpoint(_cp(2), ts="t2")
    keys = sorted(k for (_, k) in s3.objects)
    assert keys == ["prod/chain/000000000001.json", "prod/chain/000000000002.json"]
    second = json.loads(s3.objects[("audit-anchor", keys[1])])
    assert second["prev_worm_hash"] == h1 and second["worm_hash"] == h2


def test_refuses_to_overwrite_an_existing_entry(s3):
    audit_worm.anchor_checkpoint(_cp(1), ts="t1")
    # A second writer holding a stale view tries to take sequence 1 again: S3 refuses (412) and the
    # anchor retries at the next free sequence instead of clobbering the locked object.
    real = s3.list_objects_v2
    calls = {"n": 0}

    def stale_then_real(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"Contents": [], "IsTruncated": False}  # stale: looks empty
        return real(**kw)

    s3.list_objects_v2 = stale_then_real
    original = s3.objects[("audit-anchor", "prod/chain/000000000001.json")]
    audit_worm.anchor_checkpoint(_cp(2), ts="t2")
    assert s3.objects[("audit-anchor", "prod/chain/000000000001.json")] == original
    assert ("audit-anchor", "prod/chain/000000000002.json") in s3.objects


def test_verify_worm_reads_the_s3_chain(s3):
    for i in (1, 2, 3):
        audit_worm.anchor_checkpoint(_cp(i), ts=f"t{i}")
    # No DB checkpoints match these synthetic ones -> the cross-check is what fails, not the chain
    res = audit_worm.verify_worm()
    assert res["entries"] == 3
    key = ("audit-anchor", "prod/chain/000000000002.json")
    doc = json.loads(s3.objects[key])
    doc["head_hash"] = "FORGED"
    s3.objects[key] = json.dumps(doc).encode()
    res = audit_worm.verify_worm()
    assert res["ok"] is False and "broken at entry 2" in res["reason"]


def test_failure_is_counted_logged_and_degrades_to_the_fallback(s3, tmp_path, caplog):
    s3.down = True
    with caplog.at_level("ERROR"):
        res = audit_worm.anchor_checkpoint_ex(_cp(1), ts="t1")
    assert res["backend"] == "local-fallback" and res["degraded"] is True
    assert "ServiceUnavailable" in res["error"]
    assert audit_worm.anchor_failures() == {"s3": 1}
    assert "FAILED" in caplog.text
    assert (tmp_path / "fallback.jsonl").read_text().strip()
    assert not s3.objects  # nothing pretended to land in S3


def test_bucket_without_object_lock_is_a_failure_not_a_silent_success(s3):
    s3.lock_enabled = False
    res = audit_worm.anchor_checkpoint_ex(_cp(1), ts="t1")
    assert res["degraded"] is True and audit_worm.anchor_failures()["s3"] == 1
    assert not s3.objects


def test_missing_boto3_degrades_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_PATH", "s3://b/p")
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_FALLBACK_PATH", str(tmp_path / "fb.jsonl"))
    audit_worm.reset_anchor_failures()

    def no_boto3():
        raise audit_worm.WormAnchorError("boto3 not installed")

    monkeypatch.setattr(audit_worm, "_s3_client", no_boto3)
    res = audit_worm.anchor_checkpoint_ex(_cp(1), ts="t")
    assert res["degraded"] and audit_worm.anchor_failures() == {"s3": 1}


def test_bad_lock_settings_fail_closed(s3, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_S3_MODE", "SOMETIMES")
    res = audit_worm.anchor_checkpoint_ex(_cp(1), ts="t")
    assert res["degraded"] is True and not s3.objects


def test_unset_path_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_WORM_PATH", raising=False)
    assert audit_worm.anchor_checkpoint_ex(_cp(1), ts="t") is None


def test_verify_warns_about_fallback_only_checkpoints(s3, tmp_path):
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "a", "x", "t", {})
    s3.down = True
    res = audit_worm.checkpoint_and_anchor()
    assert res["status"] == "checkpointed" and res["anchored"] is False and res["degraded"]
    s3.down = False
    v = audit_worm.verify_worm()
    # the checkpoint sits only in the local fallback: cross-check accepts it but says so
    assert v["ok"] is True and "fallback" in v["warning"]


def test_checkpoint_and_anchor_end_to_end_and_skip_unchanged(s3):
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "a", "x", "t", {})
    first = audit_worm.checkpoint_and_anchor()
    assert first["status"] == "checkpointed" and first["backend"] == "s3" and first["anchored"]
    again = audit_worm.checkpoint_and_anchor(skip_if_unchanged=True)
    assert again["status"] == "unchanged"
    assert len(s3.puts) == 1  # the unchanged head was not re-anchored
    write_audit_event("cli", "a", "y", "t", {})
    third = audit_worm.checkpoint_and_anchor(skip_if_unchanged=True)
    assert third["status"] == "checkpointed" and len(s3.puts) == 2
    assert audit_worm.verify_worm()["ok"] is True


def test_unchanged_head_retries_a_failed_anchor(s3):
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "a", "x", "t", {})
    s3.down = True
    audit_worm.checkpoint_and_anchor()
    s3.down = False
    healed = audit_worm.checkpoint_and_anchor(skip_if_unchanged=True)
    assert healed["status"] == "checkpointed" and healed["anchored"] is True


def test_cli_checkpoint_anchor_flag(s3):
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "a", "x", "t", {})
    ok = runner.invoke(app, ["--json", "audit", "checkpoint", "--anchor"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["backend"] == "s3"
    write_audit_event("cli", "a", "y", "t", {})
    s3.down = True
    bad = runner.invoke(app, ["audit", "checkpoint", "--anchor"])
    assert bad.exit_code == 1  # requested an anchor, got a degraded one
    s3.down = False


def test_cli_checkpoint_without_anchor_flag_still_succeeds_but_warns(s3):
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "a", "x", "t", {})
    s3.down = True
    res = runner.invoke(app, ["audit", "checkpoint"])
    assert res.exit_code == 0 and "did not complete" in res.output
