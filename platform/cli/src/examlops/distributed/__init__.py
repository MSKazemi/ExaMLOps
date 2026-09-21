"""Next-Gen 40 · E6 — distributed & fault-tolerant training (ADR 0032).

Multi-GPU/multi-node training (FSDP / DeepSpeed ZeRO) launched through the phase-23
scheduler abstraction, with **automatic sharded checkpoint/resume** on failure/preemption
and full lineage/cost linkage.

- ``launch_distributed`` builds a torchrun/elastic rendezvous from the scheduler node list
  and records the run (degrades to a mock local launch — CPU gloo in CI, GWT-1).
- ``write_checkpoint`` writes a sharded checkpoint to durable storage keyed by run, with an
  **integrity hash** over the training state (step/epoch/optimizer), linked to MLflow/A1/A2.
- ``resume_from_checkpoint`` returns the **last integrity-valid** checkpoint (step/epoch +
  optimizer state preserved) so a resubmitted job resumes rather than restarting — and
  **refuses a corrupt** checkpoint (GWT-3/GWT-5).
- Failures and resumes are audited (D4); per-run cost feeds ``exa models cost`` (R7).

Pure-Python and fully testable — no GPU, torch, or scheduler required to launch (mock),
checkpoint, verify integrity, or resume.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from examlops import data as platform_db

STRATEGIES = ("fsdp", "zero", "megatron")


@dataclass
class LaunchSpec:
    model: str
    run_id: str
    nodes: int
    gpus_per_node: int
    strategy: str
    rdzv_endpoint: str
    nproc_per_node: int

    def torchrun_command(self) -> list[str]:
        """The real torchrun/elastic command this spec would launch (R1)."""
        return [
            "torchrun",
            f"--nnodes={self.nodes}",
            f"--nproc_per_node={self.nproc_per_node}",
            "--rdzv_backend=c10d",
            f"--rdzv_endpoint={self.rdzv_endpoint}",
            f"--rdzv_id={self.run_id}",
            "train.py",
            f"--strategy={self.strategy}",
        ]


@dataclass
class RunHandle:
    run_id: str
    model: str
    spec: LaunchSpec
    status: str = "running"


@dataclass
class Checkpoint:
    run_id: str
    step: int
    epoch: int
    state: dict[str, Any]
    integrity_hash: str
    shard_count: int = 1
    uri: str | None = None
    valid: bool = True


def _rendezvous_endpoint(node_list: list[str] | None) -> str:
    """Derive the rendezvous endpoint from the scheduler node list (R1).

    Uses the first allocated node as rank-0 host; degrades to localhost when no scheduler
    node list is available (dev/CI).
    """
    host = (node_list[0] if node_list else "localhost").split(":")[0]
    return f"{host}:29500"


def launch_distributed(
    model: str,
    nodes: int,
    strategy: str = "fsdp",
    *,
    run_id: str | None = None,
    gpus_per_node: int = 1,
    node_list: list[str] | None = None,
    dataset_revision: str | None = None,
    checkpoint_every: str | None = None,
    actor: str | None = None,
) -> RunHandle:
    """Launch (or plan) a distributed training run (R1/R2/GWT-1/GWT-4)."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")
    rid = run_id or f"dist-{model}-{nodes}x{gpus_per_node}-{strategy}"
    spec = LaunchSpec(
        model=model,
        run_id=rid,
        nodes=nodes,
        gpus_per_node=gpus_per_node,
        strategy=strategy,
        rdzv_endpoint=_rendezvous_endpoint(node_list),
        nproc_per_node=gpus_per_node,
    )
    platform_db.create_distributed_run(
        rid,
        model,
        nodes=nodes,
        gpus_per_node=gpus_per_node,
        strategy=strategy,
        dataset_revision=dataset_revision,
        checkpoint_every=checkpoint_every,
    )
    _emit_lineage(rid, model, dataset_revision)
    _audit(rid, "distributed_launch", {"model": model, "nodes": nodes, "strategy": strategy}, actor)
    return RunHandle(run_id=rid, model=model, spec=spec)


def _integrity_hash(run_id: str, step: int, epoch: int, state: dict[str, Any]) -> str:
    payload = json.dumps(
        {"run_id": run_id, "step": step, "epoch": epoch, "state": state},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def write_checkpoint(
    run_id: str,
    step: int,
    epoch: int,
    state: dict[str, Any],
    *,
    shard_count: int = 1,
    uri: str | None = None,
    mlflow_run_id: str | None = None,
) -> Checkpoint:
    """Write a sharded, integrity-hashed checkpoint to durable storage (R3/R5/GWT-2)."""
    integrity = _integrity_hash(run_id, step, epoch, state)
    platform_db.write_training_checkpoint(
        run_id,
        step,
        epoch,
        json.dumps(state),
        integrity,
        shard_count=shard_count,
        uri=uri or f"minio://checkpoints/{run_id}/step-{step}",
        mlflow_run_id=mlflow_run_id,
    )
    return Checkpoint(
        run_id=run_id,
        step=step,
        epoch=epoch,
        state=state,
        integrity_hash=integrity,
        shard_count=shard_count,
        uri=uri or f"minio://checkpoints/{run_id}/step-{step}",
    )


def verify_checkpoint(row: dict[str, Any]) -> bool:
    """Recompute the integrity hash and compare to the stored one (R5/GWT-5)."""
    state = json.loads(row["state_json"])
    expected = _integrity_hash(row["run_id"], row["step"], row["epoch"], state)
    return expected == row["integrity_hash"]


def resume_from_checkpoint(run_id: str, *, actor: str | None = None) -> Checkpoint | None:
    """Return the last **integrity-valid** checkpoint for a run, or None (R4/GWT-3/GWT-5).

    Corrupt checkpoints are skipped (refused); the newest valid one wins so training
    resumes from step/epoch + optimizer state rather than from scratch.
    """
    for row in platform_db.list_training_checkpoints(run_id):  # newest step first
        if verify_checkpoint(row):
            platform_db.update_distributed_run(run_id, status="resumed", bump_resumes=True)
            _audit(
                run_id,
                "distributed_resume",
                {"step": row["step"], "epoch": row["epoch"]},
                actor,
            )
            return Checkpoint(
                run_id=run_id,
                step=row["step"],
                epoch=row["epoch"],
                state=json.loads(row["state_json"]),
                integrity_hash=row["integrity_hash"],
                shard_count=row["shard_count"],
                uri=row["uri"],
            )
        else:
            _audit(run_id, "checkpoint_corrupt_skipped", {"step": row["step"]}, actor)
    return None


def mark_failed(run_id: str, *, reason: str = "", actor: str | None = None) -> None:
    """Record a job failure/preemption (elasticity → checkpoint-and-resubmit) (R6/GWT-6)."""
    platform_db.update_distributed_run(run_id, status="failed")
    _audit(run_id, "distributed_failed", {"reason": reason}, actor)


def complete_run(
    run_id: str, *, cost_gpu_hours: float | None = None, actor: str | None = None
) -> None:
    """Mark a run complete and record its cost (R7/GWT-6)."""
    platform_db.update_distributed_run(run_id, status="complete", cost_gpu_hours=cost_gpu_hours)
    _audit(run_id, "distributed_complete", {"cost_gpu_hours": cost_gpu_hours}, actor)


def _emit_lineage(run_id: str, model: str, dataset_rev: str | None) -> None:
    try:
        from examlops.lineage import Node, emit_lineage

        inputs = [Node(name=dataset_rev, type="dataset")] if dataset_rev else []
        emit_lineage(
            "START",
            job=f"distributed:{model}",
            run_id=run_id,
            inputs=inputs,
            outputs=[Node(name=model, type="model")],
        )
    except Exception:
        pass


def _audit(run_id: str, action: str, extra: dict[str, Any], actor: str | None) -> None:
    from examlops.data.audit import audit_best_effort

    audit_best_effort("exa-distributed", actor, action, run_id, extra)


__all__ = [
    "STRATEGIES",
    "LaunchSpec",
    "RunHandle",
    "Checkpoint",
    "launch_distributed",
    "write_checkpoint",
    "verify_checkpoint",
    "resume_from_checkpoint",
    "mark_failed",
    "complete_run",
]
