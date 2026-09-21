"""E6 through REAL ``torchrun`` — 2 gloo CPU processes (ADR 0032).

What this proves: the shipped reference script trains under DistributedDataParallel, writes sharded
checkpoints (a shard per rank + a committed manifest), survives a SIGKILLed rank by being resubmitted
and resuming from the last VALID checkpoint, reaches *bit-identical* final weights to an uninterrupted
run (seeded, batch = f(seed, step, rank), state restored exactly), and refuses a corrupt shard.

What it does NOT prove: NCCL, GPUs, multiple nodes, FSDP/DeepSpeed, or a scheduler. gloo on one CPU
host exercises the same collectives API and the same checkpoint/resume code, not the interconnect.

Skips (with a reason) when torch or gloo is unavailable; CI's unit job syncs the workspace, where
torch is a base dependency, so it runs there.

Deliberately NOT here: torchrun's in-job ``--max-restarts`` recovery. Run by hand it recovered in 5
of 6 local trials and lost the sixth to a gloo reconnect race in the restarted workers, so a test
would be flaky; the flag is passed through and its command form is unit-tested, nothing more. Three launches (plus one resubmit), ~3-4 s each; results are module-scoped.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

torch = pytest.importorskip("torch", reason="torch is not installed; distributed training needs it")
if not (torch.distributed.is_available() and torch.distributed.is_gloo_available()):
    pytest.skip("torch.distributed with the gloo backend is not available", allow_module_level=True)

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import launch  # noqa: E402

STEPS, EVERY, SEED = 10, 2, 7
FAULT = {"EXAMLOPS_DIST_FAULT_STEP": "5", "EXAMLOPS_DIST_FAULT_RANK": "1"}  # dies with 4 done


@pytest.fixture(scope="module")
def env_isolation(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("PLATFORM_DB", str(tmp_path_factory.mktemp("db") / "platform.db"))
    for k in ("EXAMLOPS_DIST_FAULT_STEP", "EXAMLOPS_SEED"):
        mp.delenv(k, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    yield
    mp.undo()


def _run(run_id, root, **kw):
    return launch.supervise(
        run_id,
        root / run_id,
        steps=STEPS,
        checkpoint_every=EVERY,
        seed=SEED,
        backoff_s=0,
        timeout_s=120,
        **kw,
    )


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    return tmp_path_factory.mktemp("runs")


@pytest.fixture(scope="module")
def clean(env_isolation, root):
    return _run("clean", root, max_attempts=1)


@pytest.fixture(scope="module")
def faulted(env_isolation, root):
    return _run("faulted", root, max_attempts=3, env_extra=FAULT)


def test_clean_run_writes_shards_manifest_and_metrics(clean):
    assert clean.status == "complete" and len(clean.attempts) == 1
    m = clean.metrics
    assert m["world_size"] == 2 and m["backend"] == "gloo" and m["steps_run"] == STEPS
    assert m["resumed_from_step"] is None and clean.resumed_from_step is None
    assert m["final_loss"] < m["first_loss"]  # it actually trained
    steps = [c["step"] for c in clean.checkpoints]
    assert steps == [2, 4, 6, 8, 10]
    d = cf.step_dir(clean.run_dir, 10)
    assert sorted(p.name for p in d.iterdir()) == [
        "manifest.json",
        "shard-0-of-2.pt",
        "shard-1-of-2.pt",
    ]  # no temp files left behind by the atomic renames
    assert cf.verify_checkpoint_dir(d).valid


def test_killed_rank_is_resubmitted_and_resumes_from_the_last_valid_checkpoint(faulted):
    assert faulted.status == "complete"
    assert [a.outcome for a in faulted.attempts] == ["recoverable", "success"]
    assert (faulted.run_dir / "fault.fired").exists()  # the fault really fired
    # Killed at the start of step 5; the newest checkpoint on disk after it was step 4.
    assert faulted.metrics["resumed_from_step"] == 4 == faulted.resumed_from_step
    assert faulted.metrics["steps_run"] == STEPS - 4
    m = cf.verify_checkpoint_dir(cf.step_dir(faulted.run_dir, 10)).manifest
    assert m["resumed_from_step"] == 4  # the run's own manifest says where it resumed


def test_resumed_run_matches_the_uninterrupted_run_exactly(clean, faulted):
    # Tolerance: none needed. Both are float32 on CPU with 2-rank gloo sums (commutative), the
    # model/momentum shards are restored byte-for-byte, and batches depend only on (seed, step,
    # rank) — so the final weights are bit-identical and the loss equal.
    assert faulted.metrics["weights_sha256"] == clean.metrics["weights_sha256"]
    assert faulted.metrics["final_loss"] == clean.metrics["final_loss"]


def test_a_corrupt_shard_is_skipped_and_the_previous_checkpoint_is_used(clean, root):
    run = root / "corrupt"
    shutil.copytree(clean.run_dir, run, ignore=shutil.ignore_patterns("attempt-*"))
    victim = cf.step_dir(run, 10) / cf.shard_name(1, 2)
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 2] ^= 0xFF  # same size, one flipped byte: only the hash can catch it
    victim.write_bytes(bytes(raw))
    res = launch.supervise(
        "corrupt",
        run,
        steps=STEPS,
        checkpoint_every=EVERY,
        seed=SEED,
        max_attempts=1,
        timeout_s=120,
    )
    assert res.status == "complete"
    assert res.metrics["resumed_from_step"] == 8
    skipped = res.metrics["skipped_checkpoints"]
    assert [s["step"] for s in skipped] == [10] and "corrupt" in skipped[0]["reason"]
    assert res.metrics["steps_run"] == 2
    assert res.metrics["weights_sha256"] == clean.metrics["weights_sha256"]  # and it heals
