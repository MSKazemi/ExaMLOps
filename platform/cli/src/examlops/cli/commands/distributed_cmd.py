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
    "  exa pipeline distributed run --local --nproc 2 --steps 12\n\n"
    "  exa pipeline distributed launch JPCP --nodes 2 --strategy fsdp\n\n"
    "  exa pipeline distributed launch JPCP --hardware-profile gpu-small\n\n"
    "  exa pipeline distributed checkpoint <run-id> --step 100 --epoch 1\n\n"
    "  exa pipeline distributed resume <run-id>\n\n"
    "  exa pipeline distributed status <run-id>\n\n"
    "  exa pipeline distributed list"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _topology(
    hardware_profile: str | None, nodes: int | None, gpus_per_node: int | None
) -> tuple[int, int]:
    """``(nodes, gpus_per_node)`` for this launch — from ``--hardware-profile`` when given.

    ADR 0157 Phase 3 / spec GWT-6: the profile is *sugar over the existing seam*. It fills the
    two topology flags before ``launch_distributed()`` is called, so a profile of 1 node × 1 GPU
    produces byte-identical arguments — and therefore an identical ``torchrun`` invocation — to
    ``--nodes 1 --gpus-per-node 1``. ``--strategy`` is not part of the resource shape and is
    never touched. An explicitly given flag still wins over the profile (a profile is a default,
    not an override — the same rule Phase 2 applies to ``--cpu``/``--memory-gb``).
    """
    if not hardware_profile:
        return (
            nodes if nodes is not None else 1,
            gpus_per_node if gpus_per_node is not None else 1,
        )

    from examlops.hardware_profiles import HardwareProfileError, resolve_for

    try:
        profile, _resolution = resolve_for(hardware_profile, "training")
    except HardwareProfileError as exc:
        _output.error(str(exc), exit_code=2)
    if nodes is not None and nodes != profile.nodes:
        _output.warning(
            f"--nodes {nodes} overrides hardware profile '{profile.name}' "
            f"v{profile.version} (nodes={profile.nodes})."
        )
    if gpus_per_node is not None and gpus_per_node != profile.gpu_count:
        _output.warning(
            f"--gpus-per-node {gpus_per_node} overrides hardware profile '{profile.name}' "
            f"v{profile.version} (gpu_count={profile.gpu_count})."
        )
    return (
        nodes if nodes is not None else profile.nodes,
        gpus_per_node if gpus_per_node is not None else profile.gpu_count,
    )


@app.command("launch", epilog=_EXAMPLES)
def launch(
    model: str = typer.Argument(..., help="Model name"),
    nodes: int = typer.Option(None, "--nodes", help="Number of nodes  [default: 1]"),
    gpus_per_node: int = typer.Option(None, "--gpus-per-node", help="GPUs per node  [default: 1]"),
    strategy: str = typer.Option("fsdp", "--strategy", help="fsdp | zero | megatron"),
    dataset_revision: str = typer.Option(None, "--dataset-revision", help="A1 revision pin"),
    checkpoint_every: str = typer.Option("10min", "--checkpoint-every", help="Checkpoint interval"),
    run_id: str = typer.Option(None, "--run-id", help="Explicit run id"),
    hardware_profile: str = typer.Option(
        None,
        "--hardware-profile",
        help="Named hardware profile (ADR 0157) supplying --nodes/--gpus-per-node; must be "
        "applicable to 'training'. Explicit flags win; --strategy is unaffected.",
    ),
) -> None:
    """Launch a distributed training run (R1/R2/R8)."""
    from examlops.distributed import launch_distributed

    nodes, gpus_per_node = _topology(hardware_profile, nodes, gpus_per_node)
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


_RUN_EXAMPLES = (
    "Examples:\n\n"
    "  exa pipeline distributed run --local\n\n"
    "  exa pipeline distributed run --local --nproc 2 --steps 12 --max-attempts 3\n\n"
    "  exa --json pipeline distributed run --local --elastic-restarts 1"
)


@app.command("run", epilog=_RUN_EXAMPLES)
def run_cmd(
    local: bool = typer.Option(
        False, "--local", help="Run on this machine (the only mode built; no scheduler submission)"
    ),
    nproc: int = typer.Option(
        2, "--nproc", min=1, help="Worker processes (torchrun nproc-per-node)"
    ),
    steps: int = typer.Option(12, "--steps", min=1, help="Training steps"),
    checkpoint_every: int = typer.Option(
        4, "--checkpoint-every", min=1, help="Steps per checkpoint"
    ),
    max_attempts: int = typer.Option(
        3, "--max-attempts", min=1, help="Submissions before giving up (recoverable failures only)"
    ),
    elastic_restarts: int = typer.Option(
        0, "--elastic-restarts", min=0, help="torchrun in-job --max-restarts (same node)"
    ),
    backoff: float = typer.Option(1.0, "--backoff", min=0.0, help="Base seconds between attempts"),
    seed: int = typer.Option(None, "--seed", help="Seed (default: EXAMLOPS_SEED, else 0)"),
    run_id: str = typer.Option(None, "--run-id", help="Explicit run id"),
) -> None:
    """Train the reference DDP script under real torchrun; resubmit and resume on failure."""
    import time

    from examlops.distributed.launch import TorchNotInstalled, default_run_dir, supervise

    if not local:
        _output.error(
            "Only --local is implemented: scheduler (Slurm/Flux) submission of distributed "
            "training is not built. Re-run with --local.",
            exit_code=2,
        )
    rid = run_id or f"dist-ref-{int(time.time())}"
    try:
        res = supervise(
            rid,
            default_run_dir(rid),
            nproc_per_node=nproc,
            steps=steps,
            checkpoint_every=checkpoint_every,
            seed=seed,
            max_attempts=max_attempts,
            max_restarts=elastic_restarts,
            backoff_s=backoff,
            actor=_actor(),
        )
    except TorchNotInstalled as exc:
        _output.error(str(exc), exit_code=2)
    if _output.json_mode:
        _output.print_json(res.to_dict())
    else:
        for a in res.attempts:
            _output.info(f"attempt {a.attempt}: {a.outcome} (exit {a.returncode}, {a.seconds}s)")
        if res.status == "complete":
            m = res.metrics or {}
            resumed = (
                f"resumed from step {res.resumed_from_step}"
                if res.resumed_from_step is not None
                else "no resume"
            )
            _output.ok(
                f"{rid} complete: {m.get('steps')} steps, final loss {m.get('final_loss'):.6g}, "
                f"{resumed} ({res.run_dir})"
            )
        else:
            _output.error(
                f"{rid} {res.status} after {len(res.attempts)} attempt(s); see {res.run_dir}"
            )
    if res.status != "complete":
        raise typer.Exit(1)
