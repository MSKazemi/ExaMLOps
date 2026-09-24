"""ADR 0109 (A) the training-checkpoint suspend backend and (B) its decision-3 consumer.

The checkpoints under test are **real**: a module-scoped fixture runs the shipped ADR 0032
reference job under real ``torchrun`` (2 gloo CPU ranks) and every test works on a copy of what it
wrote — actual shard files, an actual manifest with per-shard SHA-256. Nothing here fabricates a
manifest or re-implements hash verification; corruption is a byte flipped in a real shard, and the
eligibility decision is ``checkpoint_files``'.

The consumer half (``supervise``'s resubmit decision) needs no torch: it drives the supervisor with
a fake runner so the gate's on/off behaviour is what is being measured, not a training job.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import platform_db  # noqa: E402
from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import launch  # noqa: E402
from examlops.providers import list_providers  # noqa: E402
from examlops.suspend import (  # noqa: E402
    STATE_TRAINING_RUN,
    estimate_resume_cost,
    service,
)
from examlops.suspend.training import (  # noqa: E402
    PIN_PREFIX,
    TrainingCheckpointBackend,
    pinned_steps,
    resume_target,
)
from examlops.suspend.types import SuspendError, SuspendUnsupported  # noqa: E402

STEPS, EVERY, SEED = 6, 2, 11  # real checkpoints at steps 2, 4 and 6

torch = pytest.importorskip("torch", reason="torch is not installed; real checkpoints need it")
if not (torch.distributed.is_available() and torch.distributed.is_gloo_available()):
    pytest.skip("torch.distributed with the gloo backend is not available", allow_module_level=True)


@pytest.fixture(scope="module")
def real_run_dir(tmp_path_factory) -> Path:
    """One real ``torchrun`` DDP run; its checkpoint tree is reused (copied) by every test."""
    mp = pytest.MonkeyPatch()
    mp.setenv("PLATFORM_DB", str(tmp_path_factory.mktemp("suspenddb") / "platform.db"))
    mp.delenv("EXAMLOPS_DIST_FAULT_STEP", raising=False)
    mp.delenv(launch.PREEMPTION_GATE_ENV, raising=False)
    platform_db.init_db()
    run_dir = tmp_path_factory.mktemp("realrun") / "dist-suspend"
    res = launch.supervise(
        "dist-suspend",
        run_dir,
        steps=STEPS,
        checkpoint_every=EVERY,
        seed=SEED,
        max_attempts=1,
        backoff_s=0,
        timeout_s=300,
    )
    mp.undo()
    assert res.status == "complete", res.to_dict()
    return run_dir


@pytest.fixture
def run_dir(real_run_dir, tmp_path) -> Path:
    """A private copy of the real checkpoint tree, so a test may corrupt or delete freely."""
    dest = tmp_path / "run"
    shutil.copytree(real_run_dir, dest)
    return dest


def _actions() -> list[str]:
    with platform_db.get_db() as c:
        return [r[0] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]


def _suspend(run_dir: Path, run_id: str = "dist-suspend", **opts):
    return service.suspend(
        run_id,
        subject_kind=STATE_TRAINING_RUN,
        backend="training-checkpoint",
        options={"run_dir": str(run_dir), **opts},
    )


def _corrupt_shard(run_dir: Path, step: int, rank: int = 0) -> Path:
    """Flip one byte inside a real shard file — the manifest still names its original SHA-256."""
    d = cf.step_dir(run_dir, step)
    manifest = next(
        s for s in (cf.verify_checkpoint_dir(d).manifest["shards"]) if s["rank"] == rank
    )
    target = d / manifest["file"]
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    return target


# -- the real checkpoints the fixture produced --------------------------------------------------


def test_the_fixture_wrote_real_verified_checkpoints(run_dir):
    steps = [st.step for st in cf.list_checkpoints(run_dir) if st.valid]
    assert steps == [6, 4, 2]
    newest = cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6))
    assert newest.valid and newest.manifest["world_size"] == 2
    assert {s["rank"] for s in newest.manifest["shards"]} == {0, 1}
    assert all(len(s["sha256"]) == 64 and s["bytes"] > 0 for s in newest.manifest["shards"])


# -- capability honesty -------------------------------------------------------------------------


def test_capability_claims_only_what_the_mechanism_does():
    cap = TrainingCheckpointBackend().capability()
    assert cap.backend == "training-checkpoint"
    assert cap.granularity == "application"  # not process/container/accelerator_state
    assert cap.state_kinds == (STATE_TRAINING_RUN,)
    assert cap.tiers == ("persistent_storage",)
    assert cap.gpu_state is False and cap.peer_replication is False
    # A relaunched distributed run rebuilds its process group: the cost applies …
    assert cap.communicator_rebuild_applicable is True
    # … and nobody measured it here, so it stays unknown rather than being invented.
    assert cap.communicator_rebuild_s is None
    assert cap.restore_throughput_mb_s is None and cap.restore_fixed_s is None
    assert cap.basis == "unknown"
    notes = cap.notes.lower()
    # The notes name what is NOT persisted rather than implying a snapshot of the machine.
    assert "does not persist gpu memory" in notes.replace("\n", " ").replace("  ", " ")
    assert "process image" in notes and "communicator" in notes


def test_unknown_communicator_makes_the_resume_cost_unknown_not_optimistic():
    from dataclasses import replace

    cap = TrainingCheckpointBackend().capability()
    # Even with a throughput, the unmeasured communicator rebuild keeps the total honest.
    cost = estimate_resume_cost(replace(cap, restore_throughput_mb_s=200.0), 10 * 1024 * 1024)
    assert cost.total_s is None and cost.basis == "unknown"
    assert cost.state_transfer_s is not None  # the part we do know is still reported


def test_a_gpu_state_request_is_refused_not_pretended(run_dir):
    with pytest.raises(SuspendUnsupported, match="GPU state"):
        _suspend(run_dir, require_gpu_state=True)
    with pytest.raises(SuspendUnsupported, match="cannot persist"):
        service.suspend(
            "dist-suspend", subject_kind="agent_session_checkpoint", backend="training-checkpoint"
        )


def test_the_backend_is_registered_but_is_not_the_default():
    infos = {i.name: i for i in list_providers("suspend_backend")}
    assert "training-checkpoint" in infos and infos["training-checkpoint"].ok
    assert infos["checkpoint-only"].value == "default"
    assert infos["training-checkpoint"].value == "builtin"


# -- snapshot / restore / discard round trip ----------------------------------------------------


def test_snapshot_pins_the_newest_valid_manifest(run_dir):
    handle = _suspend(run_dir)
    on_disk = cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6)).manifest
    assert handle.pointer["step"] == 6
    assert handle.pointer["manifest_sha256"] == on_disk["manifest_sha256"]
    assert handle.pointer["world_size"] == 2 and handle.pointer["skipped"] == []
    assert handle.state_bytes == sum(s["bytes"] for s in on_disk["shards"]) > 0
    # The pin is a real marker file next to the manifest, and a pruner can see it.
    pin = cf.step_dir(run_dir, 6) / handle.pointer["pin_file"]
    assert pin.exists() and pin.name.startswith(PIN_PREFIX)
    assert pinned_steps(run_dir) == {6: [handle.snapshot_id]}
    # Pinning must not disturb the checkpoint it pins.
    assert cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6)).valid
    row = service.status(handle.snapshot_id)
    assert row["status"] == "suspended" and row["capability"]["backend"] == "training-checkpoint"


def test_restore_verifies_the_real_shards_and_reports_the_timing_split(run_dir):
    handle = _suspend(run_dir)
    report = service.resume(handle.snapshot_id, actor="alice")
    assert report.restored
    assert report.state_transfer_s is not None and report.state_transfer_s > 0
    assert report.communicator_rebuild_s is None  # applicable, unmeasured — not a fake 0.0
    assert report.total_s == pytest.approx(report.state_transfer_s)
    assert "resumes from it" in report.detail
    assert service.status(handle.snapshot_id)["status"] == "resumed"
    assert _actions() == ["suspend_snapshot", "suspend_resume"]
    assert resume_target(handle) == {
        "run_dir": str(run_dir),
        "step": 6,
        "world_size": 2,
        "config_hash": handle.pointer["config_hash"],
    }


def test_discard_releases_the_pin_and_keeps_the_training_state(run_dir):
    handle = _suspend(run_dir)
    service.discard(handle.snapshot_id)
    assert service.status(handle.snapshot_id)["status"] == "discarded"
    assert pinned_steps(run_dir) == {}
    assert not (cf.step_dir(run_dir, 6) / handle.pointer["pin_file"]).exists()
    assert cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6)).valid  # shards untouched
    assert "suspend_discard" in _actions()


def test_a_run_with_no_valid_checkpoint_is_an_error(tmp_path):
    with pytest.raises(SuspendError, match="no valid checkpoint"):
        _suspend(tmp_path / "empty")
    assert service.list_snapshots() == []


# -- a corrupt shard makes that checkpoint ineligible -------------------------------------------


def test_a_corrupt_newest_shard_falls_back_to_the_previous_valid_checkpoint(run_dir):
    _corrupt_shard(run_dir, 6, rank=1)
    status6 = cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6))
    assert not status6.valid and "corrupt" in status6.reason  # checkpoint_files' own verdict

    handle = _suspend(run_dir)
    assert handle.pointer["step"] == 4  # the previous checkpoint that still verifies
    assert [s["step"] for s in handle.pointer["skipped"]] == [6]
    assert "corrupt" in handle.pointer["skipped"][0]["reason"]
    assert service.resume(handle.snapshot_id).restored


def test_restore_refuses_a_checkpoint_corrupted_after_the_snapshot(run_dir):
    handle = _suspend(run_dir)
    _corrupt_shard(run_dir, 6, rank=0)
    report = service.resume(handle.snapshot_id)
    assert not report.restored and "no longer verifies" in report.detail
    assert report.state_transfer_s is None and report.communicator_rebuild_s is None
    assert service.status(handle.snapshot_id)["status"] == "failed"
    assert "suspend_resume_failed" in _actions()


def test_restore_refuses_a_step_that_was_rewritten_under_the_pin(run_dir):
    handle = _suspend(run_dir)
    # Re-commit step 6 with a different config hash: every shard still verifies, but it is not the
    # checkpoint that was pinned.
    manifest = cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6)).manifest
    cf.write_manifest(
        run_dir,
        step=6,
        world_size=manifest["world_size"],
        cfg_hash="a-different-config",
        shards=manifest["shards"],
        resumed_from_step=None,
        epoch=manifest["epoch"],
    )
    report = service.resume(handle.snapshot_id)
    assert not report.restored
    assert "no longer verifies" in report.detail or "rewritten" in report.detail


def test_restore_says_so_when_a_newer_checkpoint_has_overtaken_the_pin(run_dir):
    handle = _suspend(run_dir)
    manifest = cf.verify_checkpoint_dir(cf.step_dir(run_dir, 6)).manifest
    newer = cf.step_dir(run_dir, 8)
    newer.mkdir(parents=True)
    shards = []
    for s in manifest["shards"]:
        blob = (cf.step_dir(run_dir, 6) / s["file"]).read_bytes()
        cf.atomic_write_bytes(newer / s["file"], blob)
        shards.append({**s, "sha256": cf.sha256_file(newer / s["file"]), "bytes": len(blob)})
    cf.write_manifest(
        run_dir,
        step=8,
        world_size=manifest["world_size"],
        cfg_hash=manifest["config_hash"],
        shards=shards,
        resumed_from_step=6,
    )
    report = service.resume(handle.snapshot_id)
    assert report.restored and "step 8" in report.detail  # the divergence is stated, not hidden


def test_measurement_comes_only_from_recorded_restores(run_dir):
    assert service.capability("training-checkpoint").basis == "unknown"
    handle = _suspend(run_dir)
    assert service.resume(handle.snapshot_id).restored
    cap = service.capability("training-checkpoint")
    assert cap.basis == "measured" and cap.restore_throughput_mb_s
    assert cap.communicator_rebuild_s is None  # measuring the read never invents the rebuild
    assert estimate_resume_cost(cap, handle.state_bytes).total_s is None


# -- (B) the decision-3 consumer: supervise's resubmit decision ----------------------------------


def _recoverable_runner(cmd, env, log, timeout):
    Path(log).write_text("[rank 0] peer died\n")
    return cf.EXIT_RECOVERABLE


def _supervise(run_id, run_dir, **kw):
    return launch.supervise(
        run_id,
        run_dir,
        max_attempts=3,
        backoff_s=0,
        runner=_recoverable_runner,
        sleep=lambda _s: None,
        require_torch=False,
        **kw,
    )


def test_the_gate_is_off_by_default_and_changes_nothing(run_dir, monkeypatch):
    monkeypatch.delenv(launch.PREEMPTION_GATE_ENV, raising=False)
    assert launch.preemption_gate_enabled() is False
    res = _supervise("gate-off", run_dir)
    assert len(res.attempts) == 3 and res.status == "failed"  # every submission spent, as before
    assert res.preemption is None  # the gate was not consulted
    assert "distributed_resubmit_declined" not in _actions()


def test_with_the_gate_on_a_run_that_has_state_still_resubmits(run_dir, monkeypatch):
    monkeypatch.setenv(launch.PREEMPTION_GATE_ENV, "1")
    res = _supervise("gate-on-promised", run_dir)
    assert len(res.attempts) == 3  # promised: unchanged behaviour
    assert res.preemption == {
        "gate": "on",
        "promised": True,
        "reasons": res.preemption["reasons"],
    }
    assert any("newest valid checkpoint: step 6" in r for r in res.preemption["reasons"])
    assert any("application granularity" in r for r in res.preemption["reasons"])  # ceiling visible
    assert "distributed_resubmit_declined" not in _actions()


def test_with_the_gate_on_a_run_with_no_surviving_state_is_not_resubmitted(tmp_path, monkeypatch):
    monkeypatch.setenv(launch.PREEMPTION_GATE_ENV, "on")
    res = _supervise("gate-on-declined", tmp_path / "fresh")
    assert len(res.attempts) == 1  # the second submission was declined, not silently spent
    assert res.status == "failed" and res.preemption["promised"] is False
    assert any("restart from step 0" in r for r in res.preemption["reasons"])
    assert "distributed_resubmit_declined" in _actions()


def test_the_gate_declines_when_the_backend_cannot_be_resolved(run_dir, monkeypatch):
    monkeypatch.setenv(launch.PREEMPTION_GATE_ENV, "true")
    promised, reasons = launch.preemption_check("r", run_dir, backend="criu")
    assert promised is False and "unavailable" in reasons[0]


def test_a_corrupt_only_run_is_declined_by_the_gate(run_dir, monkeypatch):
    for step in (2, 4, 6):
        _corrupt_shard(run_dir, step, rank=0)
    monkeypatch.setenv(launch.PREEMPTION_GATE_ENV, "1")
    promised, reasons = launch.preemption_check("dist-suspend", run_dir)
    assert promised is False
    assert any("no checkpoint that verifies" in r for r in reasons)
