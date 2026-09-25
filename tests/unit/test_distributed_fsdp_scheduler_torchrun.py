"""ADR 0032 through REAL ``torchrun`` (2 gloo CPU ranks): FSDP2 training with sharded
checkpoint/resume, and the whole scheduler path — a generated job script submitted to the phase-23
**mock** scheduler adapter, a SIGKILLed rank, a scheduler resubmission that resumes, a durable
(NFS-directory) checkpoint store, and a fresh node restoring from it.

What this proves: FSDP (``fully_shard``) trains and resumes to *bit-identical* weights; the
scheduler-submitted script really runs torchrun; resubmission goes back through the adapter; the
durable copy is enough to resume after the scratch directory is lost.
What it does NOT prove: Slurm/Flux, multi-node rendezvous, NCCL or GPUs (the mock adapter runs the
single-node script on this host).
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
try:
    from torch.distributed.fsdp import fully_shard  # noqa: F401
except ImportError:
    pytest.skip("this torch has no FSDP2 (fully_shard)", allow_module_level=True)

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import launch, scheduled  # noqa: E402
from examlops.distributed.strategy import DistributedPlan  # noqa: E402

STEPS, EVERY, SEED = 8, 2, 11
FAULT = {"EXAMLOPS_DIST_FAULT_STEP": "5", "EXAMLOPS_DIST_FAULT_RANK": "1"}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("env")
    mp.setenv("PLATFORM_DB", str(base / "platform.db"))
    mp.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(base / "jobs"))
    mp.setenv("EXAMLOPS_HPC_WORKDIR", str(base / "hpc"))
    mp.setenv("EXAMLOPS_HPC_SCHEDULER", "mock")
    for k in (
        "EXAMLOPS_DIST_FAULT_STEP",
        "EXAMLOPS_SEED",
        "EXAMLOPS_DIST_CHECKPOINT_STORE",
        "EXAMLOPS_HPC_REMOTE_REPO",
        "EXAMLOPS_HPC_REMOTE_PYTHON",
        "EXAMLOPS_HPC_REMOTE_WORKDIR",
    ):
        mp.delenv(k, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    yield base
    mp.undo()


def _local(run_id, root, **kw):
    return launch.supervise(
        run_id,
        root / run_id,
        steps=STEPS,
        checkpoint_every=EVERY,
        seed=SEED,
        backoff_s=0,
        timeout_s=120,
        strategy="fsdp",
        **kw,
    )


@pytest.fixture(scope="module")
def fsdp_clean(env):
    return _local("fsdp-clean", env, max_attempts=1)


@pytest.fixture(scope="module")
def fsdp_faulted(env):
    return _local("fsdp-faulted", env, max_attempts=3, env_extra=FAULT)


def test_fsdp_trains_and_writes_whole_tensor_shards(fsdp_clean):
    assert fsdp_clean.status == "complete", fsdp_clean.attempts
    m = fsdp_clean.metrics
    assert m["strategy"] == "fsdp" and m["world_size"] == 2 and m["final_loss"] < m["first_loss"]
    assert [c["step"] for c in fsdp_clean.checkpoints] == [2, 4, 6, 8]
    blob = torch.load(cf.step_dir(fsdp_clean.run_dir, 8) / cf.shard_name(0, 2), weights_only=True)
    # Rank 0's shard holds whole (unsharded) tensors, so it reassembles under any world size.
    assert tuple(blob["params"]["0.weight"].shape) == (32, 16)


def test_fsdp_killed_rank_resumes_to_bit_identical_weights(fsdp_clean, fsdp_faulted):
    assert fsdp_faulted.status == "complete"
    assert [a.outcome for a in fsdp_faulted.attempts] == ["recoverable", "success"]
    assert fsdp_faulted.resumed_from_step == 4
    assert fsdp_faulted.metrics["weights_sha256"] == fsdp_clean.metrics["weights_sha256"]
    assert fsdp_faulted.metrics["final_loss"] == fsdp_clean.metrics["final_loss"]


@pytest.fixture(scope="module")
def via_scheduler(env, fsdp_clean):
    mp = pytest.MonkeyPatch()
    for k, v in FAULT.items():  # the mock adapter's job inherits the submitter's environment
        mp.setenv(k, v)
    try:
        plan = DistributedPlan(
            model="RefModel",
            strategy="fsdp",
            nproc_per_node=2,
            steps=STEPS,
            checkpoint_every=EVERY,
            max_attempts=3,
        )
        res = scheduled.supervise_scheduled(
            plan,
            "sched-fsdp",
            env / "runs" / "sched-fsdp",
            seed=SEED,
            checkpoint_store=str(env / "nfs"),
            backoff_s=0,
            poll_interval=0,
        )
    finally:
        mp.undo()
    return res


def test_the_mock_scheduler_runs_the_job_resubmits_and_resumes(via_scheduler, fsdp_clean):
    res = via_scheduler
    assert res.scheduler == "mock" and res.status == "complete", [a.__dict__ for a in res.attempts]
    assert [a.outcome for a in res.attempts] == ["recoverable", "success"]
    assert [a.state for a in res.attempts] == ["FAILED", "COMPLETED"]
    assert res.attempts[0].job_id != res.attempts[1].job_id  # two scheduler jobs
    assert res.resumed_from_step == 4
    # Same seed, same strategy: the scheduler path yields the local run's exact weights.
    assert res.metrics["weights_sha256"] == fsdp_clean.metrics["weights_sha256"]
    assert sorted(res.mirrored) == [2, 4, 6, 8]


def test_a_fresh_node_resumes_from_the_durable_store_alone(env, via_scheduler):
    # The node is gone: scratch is wiped. The next submission must restore from NFS and resume.
    shutil.rmtree(via_scheduler.run_dir)
    plan = DistributedPlan(
        model="RefModel",
        strategy="ddp",  # strategy-independent checkpoints: an FSDP checkpoint resumes under DDP
        nproc_per_node=2,
        steps=STEPS + 2,
        checkpoint_every=EVERY,
        max_attempts=1,
    )
    res = scheduled.supervise_scheduled(
        plan,
        "sched-fsdp",
        via_scheduler.run_dir,
        seed=SEED,
        checkpoint_store=str(env / "nfs"),
        backoff_s=0,
        poll_interval=0,
    )
    assert res.status == "complete" and res.restored_from_store == [8]
    assert res.metrics["resumed_from_step"] == 8 and res.metrics["steps_run"] == 2
