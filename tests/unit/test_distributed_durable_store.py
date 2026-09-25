"""ADR 0032 decision 2: checkpoints mirrored to durable storage (NFS directory / MinIO via an
fsspec URL) and restored — re-verified — into an empty run directory. No torch needed."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import durable  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv(durable.STORE_ENV, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _ckpt(run_dir: Path, step: int, world: int = 2, cfg: str = "c") -> None:
    d = cf.step_dir(run_dir, step)
    d.mkdir(parents=True, exist_ok=True)
    shards = []
    for r in range(world):
        data = f"weights-{step}-{r}".encode() * 50
        cf.atomic_write_bytes(d / cf.shard_name(r, world), data)
        shards.append(
            {
                "rank": r,
                "file": cf.shard_name(r, world),
                "sha256": cf.sha256_file(d / cf.shard_name(r, world)),
                "bytes": len(data),
            }
        )
    cf.write_manifest(
        run_dir, step=step, world_size=world, cfg_hash=cfg, shards=shards, resumed_from_step=None
    )


def _actions(target: str) -> list[str]:
    from examlops import platform_db

    with platform_db.get_db() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE target=? ORDER BY id", (target,)
            )
        ]


def test_mirror_then_restore_into_an_empty_run_dir_on_an_nfs_directory(tmp_path):
    run, nfs = tmp_path / "scratch" / "r1", tmp_path / "nfs"
    _ckpt(run, 2)
    _ckpt(run, 4)
    store = durable.open_store(str(nfs))
    res = durable.mirror_run(store, "r1", run)
    assert res.mirrored == [2, 4] and not res.failed
    # The durable copy verifies with the same verifier as the scratch copy.
    assert cf.verify_checkpoint_dir(nfs / "r1" / "ckpt" / "step-00000004", "c").valid
    # The node is lost: the scratch directory is gone. A new job restores the newest step.
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r1", run) == 4
    latest, _ = cf.find_latest_valid(run, "c")
    assert latest is not None and latest.step == 4
    assert _actions("r1") == [
        "checkpoint_mirrored",
        "checkpoint_mirrored",
        "checkpoint_restored",
    ]


def test_mirroring_is_idempotent_and_restore_is_a_noop_when_local_is_current(tmp_path):
    run = tmp_path / "r2"
    _ckpt(run, 2)
    store = durable.open_store(f"file://{tmp_path / 'store'}")
    assert durable.mirror_run(store, "r2", run).mirrored == [2]
    again = durable.mirror_run(store, "r2", run)
    assert again.mirrored == [] and again.already == [2]
    assert durable.restore_latest(store, "r2", run) is None  # local is already at step 2


def test_a_corrupt_durable_copy_is_rejected_and_the_older_one_restored(tmp_path):
    run, nfs = tmp_path / "r3", tmp_path / "nfs"
    _ckpt(run, 2)
    _ckpt(run, 4)
    store = durable.open_store(str(nfs))
    durable.mirror_run(store, "r3", run)
    victim = nfs / "r3" / "ckpt" / "step-00000004" / cf.shard_name(1, 2)
    raw = bytearray(victim.read_bytes())
    raw[3] ^= 0xFF  # same size: only the hash can see it
    victim.write_bytes(bytes(raw))
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r3", run) == 2
    assert not cf.step_dir(run, 4).exists()  # the rejected copy never stays in the run dir
    assert "checkpoint_restore_rejected" in _actions("r3")


def test_an_uncommitted_upload_is_ignored(tmp_path):
    run, nfs = tmp_path / "r4", tmp_path / "nfs"
    _ckpt(run, 2)
    store = durable.open_store(str(nfs))
    durable.mirror_run(store, "r4", run)
    half = nfs / "r4" / "ckpt" / "step-00000006"
    half.mkdir(parents=True)
    (half / cf.shard_name(0, 2)).write_bytes(b"partial")  # shards but no manifest yet
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r4", run) == 2


def test_a_hostile_manifest_cannot_write_outside_the_run_dir(tmp_path):
    run, nfs = tmp_path / "r5", tmp_path / "nfs"
    _ckpt(run, 2)
    store = durable.open_store(str(nfs))
    durable.mirror_run(store, "r5", run)
    mpath = nfs / "r5" / "ckpt" / "step-00000002" / cf.MANIFEST
    body = json.loads(mpath.read_text())
    body["shards"][0]["file"] = "../../../escape.pt"
    mpath.write_text(json.dumps(body))
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r5", run) is None
    assert not (tmp_path / "escape.pt").exists()
    assert "checkpoint_restore_failed" in _actions("r5")


def test_an_fsspec_url_store_round_trips(tmp_path):
    pytest.importorskip("fsspec")
    run = tmp_path / "r6"
    _ckpt(run, 8)
    store = durable.open_store("memory://examlops-test-ckpt")
    assert durable.mirror_run(store, "r6", run).mirrored == [8]
    assert store.step_uri("r6", 8) == "memory://examlops-test-ckpt/r6/ckpt/step-00000008"
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r6", run) == 8
    assert cf.verify_checkpoint_dir(cf.step_dir(run, 8), "c").valid


def test_a_configured_but_unusable_store_fails_closed(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(durable.DurableStoreUnavailable, match="not writable"):
        durable.open_store(str(blocker / "sub"))
    with pytest.raises(durable.DurableStoreUnavailable):
        durable.open_store("nosuchproto-xyz://bucket/x")
    monkeypatch.setenv(durable.STORE_ENV, str(tmp_path / "envstore"))
    assert durable.resolve_store(None) is not None
    monkeypatch.setenv(durable.STORE_ENV, "")
    assert durable.resolve_store(None) is None


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 200])
def test_run_ids_that_could_escape_the_store_are_refused(tmp_path, bad):
    store = durable.open_store(str(tmp_path / "s"))
    with pytest.raises(ValueError, match="not a safe store key"):
        store.step_key(bad, 1)


def test_a_failed_upload_is_recorded_not_raised_and_retried_next_time(tmp_path, monkeypatch):
    run = tmp_path / "r7"
    _ckpt(run, 2)
    store = durable.open_store(str(tmp_path / "nfs"))
    real = store.put_file
    monkeypatch.setattr(store, "put_file", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    res = durable.mirror_run(store, "r7", run)
    assert res.mirrored == [] and res.failed[0]["step"] == 2
    assert "checkpoint_mirror_failed" in _actions("r7")
    monkeypatch.setattr(store, "put_file", real)
    assert durable.mirror_run(store, "r7", run).mirrored == [2]
    fn = durable.uri_for(store, "r7", only={2})
    assert fn is not None and fn(2).endswith("step-00000002") and fn(4) is None


def test_a_lost_mirror_or_restore_audit_is_counted(tmp_path, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    run = tmp_path / "r8"
    _ckpt(run, 2)
    store = durable.open_store(str(tmp_path / "nfs"))
    assert durable.mirror_run(store, "r8", run).mirrored == [2]  # the copy still happens
    shutil.rmtree(run)
    assert durable.restore_latest(store, "r8", run) == 2
    dropped = dropped_audit_events()
    assert dropped.get("checkpoint_mirrored") == 1 and dropped.get("checkpoint_restored") == 1
    reset_dropped_audit_events()
