"""ADR 0109 decision 8 — the node-local replica tier of ``tiered-training-checkpoint``.

The checkpoints are written with the shipped ADR 0032 writer (``checkpoint_files.write_manifest``
over real shard files with real SHA-256), so every integrity verdict below is
``verify_checkpoint_dir``'s — the same code a training job trusts. No torch is needed: the shard
*contents* are opaque bytes to the integrity layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examlops import platform_db
from examlops.distributed import checkpoint_files as cf
from examlops.suspend import STATE_TRAINING_RUN, preemption_promise, service
from examlops.suspend.report import seam_report
from examlops.suspend.tiers import (
    LOCAL_DIR_ENV,
    LOCAL_MAX_ENV,
    LocalTier,
    LocalTierError,
    TieredTrainingCheckpointBackend,
    _mount_fstype,
    run_key,
)
from examlops.suspend.types import SuspendError

CFG = "cfg-hash-1"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_SUSPEND_BACKEND_PROVIDER", raising=False)
    monkeypatch.delenv(LOCAL_MAX_ENV, raising=False)
    platform_db.init_db()


def _write_step(run_dir: Path, step: int, shards: list[bytes]) -> None:
    d = cf.step_dir(run_dir, step)
    d.mkdir(parents=True, exist_ok=True)
    recs = []
    for rank, data in enumerate(shards):
        name = cf.shard_name(rank, len(shards))
        (d / name).write_bytes(data)
        recs.append(
            {"rank": rank, "file": name, "sha256": cf.sha256_file(d / name), "bytes": len(data)}
        )
    cf.write_manifest(
        run_dir,
        step=step,
        world_size=len(shards),
        cfg_hash=CFG,
        shards=recs,
        resumed_from_step=None,
    )


@pytest.fixture
def run_dir(tmp_path) -> Path:
    rd = tmp_path / "persistent" / "run-a"
    _write_step(rd, 2, [b"A" * 1000, b"B" * 1000])
    return rd


@pytest.fixture
def local_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "local"
    monkeypatch.setenv(LOCAL_DIR_ENV, str(root))
    return root


def _snap(backend, subject="run-a", run_dir=None):
    return backend.snapshot(STATE_TRAINING_RUN, subject, options={"run_dir": str(run_dir)})


def _actions() -> list[str]:
    with platform_db.get_db() as c:
        return [r[0] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]


# -- capability ---------------------------------------------------------------------------------


def test_capability_without_a_local_tier_reports_persistent_only(monkeypatch):
    monkeypatch.delenv(LOCAL_DIR_ENV, raising=False)
    cap = TieredTrainingCheckpointBackend().capability()
    assert cap.tiers == ("persistent_storage",)
    assert LOCAL_DIR_ENV in cap.notes
    assert cap.peer_replication is False and cap.communicator_rebuild_s is None


def test_capability_reports_the_measured_local_tier_label(local_root):
    cap = TieredTrainingCheckpointBackend().capability()
    assert cap.tiers[1] == "persistent_storage"
    assert cap.tiers[0] in ("local_memory", "local_storage")
    assert preemption_promise(cap).can_promise  # the persistent tier survives node loss


def test_tier_label_follows_the_filesystem_not_a_declaration(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        f"/dev/sda1 / ext4 rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\ntmpfs {tmp_path}/ram tmpfs rw 0 0\n"
    )
    assert _mount_fstype(tmp_path / "ram" / "x", str(mounts)) == "tmpfs"
    assert _mount_fstype(tmp_path / "disk", str(mounts)) == "ext4"
    assert _mount_fstype(tmp_path, str(tmp_path / "missing")) is None


# -- staging is differential and verified ------------------------------------------------------


def test_snapshot_stages_a_verified_replica(run_dir, local_root):
    h = _snap(TieredTrainingCheckpointBackend(), run_dir=run_dir)
    lt = h.pointer["local_tier"]
    assert lt["staged"] is True and lt["copied_bytes"] == 2000 and lt["reused_bytes"] == 0
    replica = cf.step_dir(Path(lt["run_dir"]), 2)
    assert cf.verify_checkpoint_dir(replica, CFG).valid
    assert (cf.step_dir(run_dir, 2) / f".suspend-pin-{h.snapshot_id}.json").exists()


def test_second_snapshot_copies_only_changed_shards(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    _snap(b, run_dir=run_dir)
    _write_step(run_dir, 4, [b"A" * 1000, b"C" * 1500])  # rank 0 unchanged
    h2 = _snap(b, run_dir=run_dir)
    lt = h2.pointer["local_tier"]
    assert h2.pointer["step"] == 4
    assert lt["copied_bytes"] == 1500 and lt["reused_bytes"] == 1000
    objects = sorted(p.name for p in (Path(lt["run_dir"]) / "objects").iterdir())
    assert len(objects) == 3  # A, B, C — the shared A shard is held once


def test_cap_exceeded_degrades_to_persistent_only(run_dir, local_root, monkeypatch):
    monkeypatch.setenv(LOCAL_MAX_ENV, "1500")
    h = _snap(TieredTrainingCheckpointBackend(), run_dir=run_dir)
    assert h.pointer["local_tier"]["staged"] is False
    assert "cap exceeded" in h.pointer["local_tier"]["reason"]
    rep = TieredTrainingCheckpointBackend().restore(h)
    assert rep.restored and rep.detail.startswith("persistent tier")


def test_invalid_cap_is_a_clear_error(local_root, monkeypatch):
    monkeypatch.setenv(LOCAL_MAX_ENV, "lots")
    with pytest.raises(LocalTierError, match="integer"):
        LocalTier(local_root)


def test_stage_refuses_a_source_that_does_not_verify(run_dir, tmp_path):
    shard = cf.step_dir(run_dir, 2) / cf.shard_name(0, 2)
    shard.write_bytes(b"X" * 1000)
    with pytest.raises(LocalTierError, match="does not verify"):
        LocalTier(tmp_path / "l").stage("k", cf.step_dir(run_dir, 2), "sid")


# -- restore prefers the local tier, falls back honestly ---------------------------------------


def test_restore_is_served_from_the_local_tier(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    h = _snap(b, run_dir=run_dir)
    rep = b.restore(h)
    assert rep.restored and rep.state_transfer_s is not None and rep.state_transfer_s > 0
    assert rep.communicator_rebuild_s is None  # applicable, unmeasured — never invented
    assert "served from the" in rep.detail and "tier" in rep.detail


def test_localized_fault_recovers_from_the_local_replica(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    h = _snap(b, run_dir=run_dir)
    # the persistent copy is damaged; the node-local replica is intact
    (cf.step_dir(run_dir, 2) / cf.shard_name(1, 2)).write_bytes(b"Z" * 1000)
    rep = b.restore(h)
    assert rep.restored and "served from the" in rep.detail


def test_corrupt_replica_falls_back_to_persistent_and_says_why(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    h = _snap(b, run_dir=run_dir)
    replica = cf.step_dir(Path(h.pointer["local_tier"]["run_dir"]), 2)
    target = replica / cf.shard_name(0, 2)
    target.unlink()  # break the link without touching the shared object
    target.write_bytes(b"Q" * 1000)
    rep = b.restore(h)
    assert rep.restored
    assert rep.detail.startswith("persistent tier (local tier unavailable: shard 0 corrupt)")


def test_both_tiers_broken_is_a_failed_restore(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    h = _snap(b, run_dir=run_dir)
    import shutil

    shutil.rmtree(Path(h.pointer["local_tier"]["run_dir"]))
    (cf.step_dir(run_dir, 2) / cf.shard_name(0, 2)).write_bytes(b"Z" * 1000)
    rep = b.restore(h)
    assert rep.restored is False and "no longer verifies" in rep.detail


# -- discard releases pins and garbage-collects ------------------------------------------------


def test_discard_releases_replica_and_gcs_orphans_but_keeps_the_checkpoint(run_dir, local_root):
    b = TieredTrainingCheckpointBackend()
    h1 = _snap(b, run_dir=run_dir)
    _write_step(run_dir, 4, [b"A" * 1000, b"C" * 1500])
    h2 = _snap(b, run_dir=run_dir)
    local_run = Path(h1.pointer["local_tier"]["run_dir"])
    b.discard(h1)
    assert not cf.step_dir(local_run, 2).exists()
    remaining = {p.name for p in (local_run / "objects").iterdir()}
    assert len(remaining) == 2  # B is orphaned and freed; A (shared with step 4) and C stay
    assert cf.verify_checkpoint_dir(cf.step_dir(run_dir, 2), CFG).valid  # never deleted
    assert not (cf.step_dir(run_dir, 2) / f".suspend-pin-{h1.snapshot_id}.json").exists()
    b.discard(h2)
    assert not any((local_run / "objects").iterdir())


def test_run_key_is_stable_and_filesystem_safe(tmp_path):
    k1 = run_key("run/../x", tmp_path / "a")
    assert "/" not in k1 and k1 == run_key("run/../x", tmp_path / "a")
    assert k1 != run_key("run/../x", tmp_path / "b")


# -- through the audited service and the status report -----------------------------------------


def test_service_round_trip_is_audited_and_reported(run_dir, local_root):
    h = service.suspend(
        "run-a",
        subject_kind=STATE_TRAINING_RUN,
        backend="tiered-training-checkpoint",
        options={"run_dir": str(run_dir)},
        tenant="t1",
        actor="alice",
    )
    rep = service.resume(h.snapshot_id, actor="alice")
    assert rep.restored
    assert _actions()[-2:] == ["suspend_snapshot", "suspend_resume"]
    report = seam_report()
    tiered = next(b for b in report["backends"] if b["backend"] == "tiered-training-checkpoint")
    assert tiered["timing"]["restores"] == 1
    assert tiered["timing"]["last_state_transfer_s"] == pytest.approx(rep.state_transfer_s)
    assert tiered["capability"]["basis"] == "measured"
    assert report["snapshots"] == {"resumed": 1}
    stored = service.status(h.snapshot_id)
    assert stored is not None and stored["pointer"]["local_tier"]["staged"] is True
    json.dumps(report)  # JSON-safe for `exa --json status`


def test_no_valid_checkpoint_is_refused_not_staged(tmp_path, local_root):
    with pytest.raises(SuspendError, match="no valid checkpoint"):
        _snap(TieredTrainingCheckpointBackend(), run_dir=tmp_path / "empty")
    assert not local_root.exists() or not any(local_root.iterdir())


# -- review regressions: a misconfigured or failing tier degrades, it never leaks ---------------


def test_misconfigured_cap_degrades_after_the_pin_instead_of_leaking_it(
    run_dir, local_root, monkeypatch
):
    """The persistent pin is written before the tier is consulted, so a bad cap must degrade."""
    monkeypatch.setenv(LOCAL_MAX_ENV, "lots")
    cap = TieredTrainingCheckpointBackend().capability()  # must not raise
    assert cap.tiers == ("persistent_storage",) and "misconfigured" in cap.notes
    h = service.suspend(
        "run-a",
        subject_kind=STATE_TRAINING_RUN,
        backend="tiered-training-checkpoint",
        options={"run_dir": str(run_dir)},
    )
    lt = h.pointer["local_tier"]
    assert lt["staged"] is False and "misconfigured" in lt["reason"]
    stored = service.status(h.snapshot_id)
    assert stored is not None and stored["status"] == "suspended"  # the pin has a record
    pins = [p.name for p in cf.step_dir(run_dir, 2).glob(".suspend-pin-*.json")]
    assert pins == [f".suspend-pin-{h.snapshot_id}.json"]


def test_cap_refusal_leaves_no_half_staged_step(run_dir, local_root, monkeypatch):
    monkeypatch.setenv(LOCAL_MAX_ENV, "1500")
    h = _snap(TieredTrainingCheckpointBackend(), run_dir=run_dir)
    assert h.pointer["local_tier"]["staged"] is False
    key_dir = local_root / h.pointer["local_tier"]["key"]
    assert not cf.step_dir(key_dir, 2).exists()


def test_a_copy_failing_midway_removes_the_partial_replica(run_dir, local_root, monkeypatch):
    """Shard links left behind would hold bytes the cap cannot see and nothing would release."""
    import examlops.suspend.tiers as tiers_mod

    real_copy, calls = tiers_mod.shutil.copyfile, {"n": 0}

    def flaky(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(tiers_mod.shutil, "copyfile", flaky)
    h = _snap(TieredTrainingCheckpointBackend(), run_dir=run_dir)
    lt = h.pointer["local_tier"]
    assert lt["staged"] is False and "disk full" in lt["reason"]
    key_dir = local_root / lt["key"]
    assert not cf.step_dir(key_dir, 2).exists()
    assert not any((key_dir / "objects").iterdir())  # the one copied object was GC'd


def test_gc_reads_manifests_and_never_rehashes_replicas(run_dir, local_root, monkeypatch):
    b = TieredTrainingCheckpointBackend()
    h1 = _snap(b, run_dir=run_dir)
    _write_step(run_dir, 4, [b"A" * 1000, b"C" * 1500])
    _snap(b, run_dir=run_dir)
    local_run = Path(h1.pointer["local_tier"]["run_dir"])
    import examlops.suspend.tiers as tiers_mod

    def boom(*a, **kw):
        raise AssertionError("gc must not re-hash replicas")

    monkeypatch.setattr(tiers_mod.cf, "verify_checkpoint_dir", boom)
    monkeypatch.setattr(tiers_mod.cf, "list_checkpoints", boom)
    LocalTier(local_root).release(h1.pointer["local_tier"]["key"], 2, h1.snapshot_id)
    assert len(list((local_run / "objects").iterdir())) == 2  # A and C still referenced


def test_a_concurrent_gc_cannot_delete_an_in_flight_copy(run_dir, local_root, monkeypatch):
    """GC deletes every object no manifest references yet - including a copy another process is
    still staging - unless stage and GC are serialised on the tier."""
    import threading

    import examlops.suspend.tiers as tiers_mod

    real_copy = tiers_mod.shutil.copyfile
    key = run_key("run-a", run_dir)
    workers: list[threading.Thread] = []

    def racing(src, dst, *a, **kw):
        out = real_copy(src, dst, *a, **kw)
        if ".tmp." in str(dst) and not workers:
            t = threading.Thread(target=LocalTier(local_root).gc, args=(key,), daemon=True)
            workers.append(t)
            t.start()
            t.join(timeout=0.5)  # unlocked: GC runs now and deletes the tmp; locked: it waits
        return out

    monkeypatch.setattr(tiers_mod.shutil, "copyfile", racing)
    h = _snap(TieredTrainingCheckpointBackend(), run_dir=run_dir)
    workers[0].join(timeout=5)
    assert not workers[0].is_alive()
    lt = h.pointer["local_tier"]
    assert lt["staged"] is True, lt
    rep = TieredTrainingCheckpointBackend().restore(h)
    assert rep.restored and "local" in rep.detail
