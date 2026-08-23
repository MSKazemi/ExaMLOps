"""E6 — `exa pipeline distributed`: multi-node training + checkpoint/resume (ADR 0032).

Launch FSDP/DeepSpeed-ZeRO training across scheduler-allocated nodes, write integrity-hashed
sharded checkpoints, and auto-resume from the last valid checkpoint on failure/preemption.
Cost is recorded; failures/resumes are audited (D4).
"""

from __future__ import annotations

import json
import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Distributed & fault-tolerant training — FSDP/ZeRO + checkpoint/resume (E6)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa pipeline distributed launch JPCP --nodes 2 --strategy fsdp\n\n"
    "  exa pipeline distributed checkpoint <run-id> --step 100 --epoch 1\n\n"
    "  exa pipeline distributed resume <run-id>\n\n"
    "  exa pipeline distributed status <run-id>\n\n"
    "  exa pipeline distributed list"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("launch", epilog=_EXAMPLES)
def launch(
    model: str = typer.Argument(..., help="Model name"),
    nodes: int = typer.Option(1, "--nodes", help="Number of nodes"),
    gpus_per_node: int = typer.Option(1, "--gpus-per-node", help="GPUs per node"),
    strategy: str = typer.Option("fsdp", "--strategy", help="fsdp | zero | megatron"),
    dataset_revision: str = typer.Option(None, "--dataset-revision", help="A1 revision pin"),
    checkpoint_every: str = typer.Option("10min", "--checkpoint-every", help="Checkpoint interval"),
    run_id: str = typer.Option(None, "--run-id", help="Explicit run id"),
) -> None:
    """Launch a distributed training run (R1/R2/R8)."""
    from examlops.distributed import launch_distributed

    try:
        handle = launch_distributed(
            model,
            nodes,
            strategy,
            run_id=run_id,
            gpus_per_node=gpus_per_node,
            dataset_revision=dataset_revision,
            checkpoint_every=checkpoint_every,
            actor=_actor(),
        )
    except ValueError as exc:
        _output.error(str(exc))
        raise typer.Exit(2) from exc
    if _output.json_mode:
        _output.print_json(
            {
                "run_id": handle.run_id,
                "model": handle.model,
                "strategy": handle.spec.strategy,
                "nodes": handle.spec.nodes,
                "torchrun": handle.spec.torchrun_command(),
            }
        )
        return
    _output.ok(
        f"Launched {handle.run_id} — {nodes}×{gpus_per_node} GPUs, {strategy}, "
        f"rdzv {handle.spec.rdzv_endpoint}"
    )
    _output.info("  " + " ".join(handle.spec.torchrun_command()))


@app.command("checkpoint")
def checkpoint(
    run_id: str = typer.Argument(..., help="Run id"),
    step: int = typer.Option(..., "--step", help="Training step"),
    epoch: int = typer.Option(..., "--epoch", help="Training epoch"),
    shards: int = typer.Option(1, "--shards", help="Number of shards"),
    state: str = typer.Option(None, "--state", help="JSON optimizer/model state summary"),
) -> None:
    """Write an integrity-hashed sharded checkpoint (R3/R5)."""
    from examlops.distributed import write_checkpoint

    payload = json.loads(state) if state else {"step": step, "epoch": epoch}
    ckpt = write_checkpoint(run_id, step, epoch, payload, shard_count=shards)
    if _output.json_mode:
        _output.print_json(
            {"run_id": run_id, "step": ckpt.step, "uri": ckpt.uri, "hash": ckpt.integrity_hash}
        )
        return
    _output.ok(
        f"Checkpoint step {step} epoch {epoch} → {ckpt.uri} "
        f"(hash {ckpt.integrity_hash[:16]}…, {shards} shard(s))"
    )


@app.command("resume")
def resume(
    run_id: str = typer.Argument(..., help="Run id"),
) -> None:
    """Resume from the last integrity-valid checkpoint (R4/GWT-3). Exit 1 if none valid."""
    from examlops.distributed import resume_from_checkpoint

    ckpt = resume_from_checkpoint(run_id, actor=_actor())
    if ckpt is None:
        _output.error(f"No valid checkpoint for {run_id} — cannot resume (would restart).")
        raise typer.Exit(1)
    if _output.json_mode:
        _output.print_json(
            {"run_id": run_id, "step": ckpt.step, "epoch": ckpt.epoch, "uri": ckpt.uri}
        )
        return
    _output.ok(
        f"Resuming {run_id} from step {ckpt.step} epoch {ckpt.epoch} ({ckpt.uri}) — "
        "optimizer state preserved."
    )


@app.command("status")
def status(
    run_id: str = typer.Argument(..., help="Run id"),
) -> None:
    """Show a distributed run + its checkpoints."""
    from examlops.data.audit import list_training_checkpoints
    from examlops.data.data_assets import get_distributed_run

    run = get_distributed_run(run_id)
    if not run:
        _output.error(f"No distributed run {run_id}.")
        raise typer.Exit(1)
    ckpts = list_training_checkpoints(run_id)
    if _output.json_mode:
        _output.print_json({"run": run, "checkpoints": ckpts})
        return
    _output.print_record(
        {
            "run_id": run["run_id"],
            "model": run["model"],
            "topology": f"{run['nodes']}×{run['gpus_per_node']} GPUs",
            "strategy": run["strategy"],
            "status": run["status"],
            "resumes": run["resumes"],
            "cost_gpu_hours": run["cost_gpu_hours"] if run["cost_gpu_hours"] is not None else "—",
            "checkpoints": len(ckpts),
        }
    )
    if ckpts:
        _output.print_table(
            "Checkpoints",
            ["Step", "Epoch", "Shards", "URI"],
            [
                [str(c["step"]), str(c["epoch"]), str(c["shard_count"]), (c["uri"] or "")[:40]]
                for c in ckpts
            ],
        )


@app.command("list")
def list_cmd(
    model: str = typer.Argument(None, help="Filter by model"),
) -> None:
    """List distributed training runs."""
    from examlops.data.data_assets import list_distributed_runs

    runs = list_distributed_runs(model)
    if _output.json_mode:
        _output.print_json(runs)
        return
    if not runs:
        _output.info(
            "No distributed runs. Launch one with: exa pipeline distributed launch <model>"
        )
        return
    _output.print_table(
        "Distributed Runs",
        ["Run", "Model", "Topology", "Strategy", "Status", "Resumes"],
        [
            [
                r["run_id"],
                r["model"],
                f"{r['nodes']}×{r['gpus_per_node']}",
                r["strategy"],
                r["status"],
                str(r["resumes"]),
            ]
            for r in runs
        ],
    )
