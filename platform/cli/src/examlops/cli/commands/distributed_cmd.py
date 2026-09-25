"""E6 — `exa pipeline distributed`: multi-node training + checkpoint/resume (ADR 0032).

Launch FSDP/DeepSpeed-ZeRO training across scheduler-allocated nodes, write integrity-hashed
sharded checkpoints, and auto-resume from the last valid checkpoint on failure/preemption.
Cost is recorded; failures/resumes are audited (D4).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

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
    hardware_profile: str | None,
    nodes: int | None,
    gpus_per_node: int | None,
    model: str | None = None,
) -> tuple[int, int]:
    """``(nodes, gpus_per_node)`` for this launch — from ``--hardware-profile`` when given.

    ADR 0157 Phase 3 / spec GWT-6: the profile is *sugar over the existing seam*. It fills the
    two topology flags before ``launch_distributed()`` is called, so a profile of 1 node × 1 GPU
    produces byte-identical arguments — and therefore an identical ``torchrun`` invocation — to
    ``--nodes 1 --gpus-per-node 1``. ``--strategy`` is not part of the resource shape and is
    never touched. An explicitly given flag still wins over the profile (a profile is a default,
    not an override — the same rule Phase 2 applies to ``--cpu``/``--memory-gb``).

    Phase 4: when ``model`` is given, the resolution is recorded in the profile ledger against it
    (``exa hardware profile in-use`` / ``exa status`` then show what this launch was sized by).
    """
    if not hardware_profile:
        return (
            nodes if nodes is not None else 1,
            gpus_per_node if gpus_per_node is not None else 1,
        )

    from examlops.hardware_profiles import HardwareProfileError, resolve_for

    try:
        profile, _resolution = resolve_for(
            hardware_profile, "training", consumer_ref=model, actor=_actor()
        )
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

    nodes, gpus_per_node = _topology(hardware_profile, nodes, gpus_per_node, model)
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
    "  exa pipeline distributed run --local --strategy fsdp\n\n"
    "  exa pipeline distributed run --scheduler --model JPCP\n\n"
    "  exa pipeline distributed run --scheduler --model JPCP --checkpoint-store s3://ckpt/dist\n\n"
    "  exa --json pipeline distributed run --local --elastic-restarts 1"
)

_RUN_STRATEGIES = ("ddp", "fsdp", "zero", "megatron")


def print_scheduled_result(res: Any) -> None:
    """Output shared by `distributed run --scheduler` and `pipeline run --distributed`."""
    if _output.json_mode:
        _output.print_json(res.to_dict())
        if res.status != "complete":
            raise typer.Exit(1)
        return
    for a in res.attempts:
        _output.info(f"attempt {a.attempt}: {a.outcome} (job {a.job_id}, {a.state}, {a.seconds}s)")
    if res.restored_from_store:
        _output.info(f"restored from durable store: steps {res.restored_from_store}")
    if res.status != "complete":
        detail = f": {res.error}" if res.error else ""
        _output.error(
            f"{res.run_id} {res.status} after {len(res.attempts)} attempt(s){detail}; "
            f"see {res.run_dir}"
        )
    m = res.metrics or {}
    resumed = (
        f"resumed from step {res.resumed_from_step}"
        if res.resumed_from_step is not None
        else "no resume"
    )
    cost = res.cost or {}
    _output.ok(
        f"{res.run_id} complete on {res.scheduler}: {m.get('steps')} steps "
        f"({res.plan.get('strategy')}, {res.plan.get('nnodes')} node(s)), {resumed}; "
        f"cost {cost.get('gpu_hours', 0)} GPU-h / {cost.get('cpu_hours', 0)} CPU-h"
    )


def run_scheduled(
    model: str,
    *,
    run_id: str | None = None,
    overrides: dict[str, Any] | None = None,
    seed: int | None = None,
    checkpoint_store: str | None = None,
    dataset_revision: str | None = None,
    mlflow_run_id: str | None = None,
    backoff: float | None = None,
    project: str | None = None,
    gpus: int = 0,
    require_entrypoint: bool = False,
) -> None:
    """Resolve ``model``'s plan (YAML ``distributed:`` block + flags); submit it via the scheduler.

    The submission is held under the admission gate (ADR 0116) for the plan's whole GPU footprint
    (``nodes × gpus_per_node``, or ``gpus`` when that is larger): every scheduler path that starts
    training goes through it, not only ``exa pipeline run``. ``require_entrypoint`` refuses a plan
    that would run the synthetic reference script — ``exa pipeline run --distributed`` trains *the
    model*, and recording the reference script's cost, lineage and MLflow tags under the model's
    name would be a false record.
    """
    import time

    from examlops.cli._admission_gate import pipeline_run_gate
    from examlops.distributed.durable import DurableStoreUnavailable
    from examlops.distributed.scheduled import SchedulerSubmitError, supervise_scheduled
    from examlops.distributed.strategy import StrategyUnavailable, model_declared, resolve_plan

    if not model_declared(model):
        # Otherwise an unknown or typo'd name resolves to the defaults and the run's cost, lineage
        # and checkpoints are recorded under a model that does not exist.
        _output.error(
            f"No model YAML named {model!r} in the active use-case pack "
            "(EXAMLOPS_USECASE_DIR / RAY_MODELS_DIR).",
            exit_code=2,
        )
    try:
        plan = resolve_plan(model, overrides=overrides)
    except ValueError as exc:
        _output.error(str(exc), exit_code=2)
    if require_entrypoint and not plan.entrypoint:
        _output.error(
            f"{model} declares no distributed.entrypoint, so a distributed run would train the "
            "synthetic reference script, not this model. Add `entrypoint:` to its YAML "
            "`distributed:` block, or smoke-test the plan with: "
            f"exa pipeline distributed run --scheduler --model {model}",
            exit_code=2,
        )
    # The run id keys the run directory and the durable store: two runs started in the same second
    # must not share it (both would write — and resume from — the same checkpoints).
    rid = run_id or f"dist-{model.lower()}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    kwargs: dict[str, Any] = {} if backoff is None else {"backoff_s": backoff}
    try:
        with pipeline_run_gate(
            project=project, model=model, gpus=max(gpus, plan.nodes * plan.gpus_per_node)
        ):
            res = supervise_scheduled(
                plan,
                rid,
                seed=seed,
                checkpoint_store=checkpoint_store,
                dataset_revision=dataset_revision,
                mlflow_run_id=mlflow_run_id,
                actor=_actor(),
                **kwargs,
            )
    except (StrategyUnavailable, DurableStoreUnavailable, SchedulerSubmitError, ValueError) as exc:
        _output.error(str(exc), exit_code=2)
    print_scheduled_result(res)


@app.command("run", epilog=_RUN_EXAMPLES)
def run_cmd(
    local: bool = typer.Option(False, "--local", help="Run torchrun on this machine"),
    scheduler: bool = typer.Option(
        False,
        "--scheduler",
        help="Submit through the configured scheduler (EXAMLOPS_HPC_SCHEDULER: mock, slurm or "
        "flux); a recoverable failure is resubmitted there",
    ),
    model: str = typer.Option(
        None, "--model", "-m", help="Model whose YAML `distributed:` block supplies the plan"
    ),
    strategy: str = typer.Option(
        None, "--strategy", help="ddp | fsdp | zero | megatron  [default: YAML, else ddp locally]"
    ),
    nodes: int = typer.Option(None, "--nodes", min=1, help="Nodes (with --scheduler)"),
    min_nodes: int = typer.Option(
        None, "--min-nodes", min=1, help="Elastic lower bound (< --nodes enables torch elastic)"
    ),
    nproc: int = typer.Option(
        None, "--nproc", min=1, help="Worker processes per node  [default: 2 locally]"
    ),
    steps: int = typer.Option(None, "--steps", min=1, help="Training steps  [default: 12]"),
    checkpoint_every: int = typer.Option(
        None, "--checkpoint-every", min=1, help="Steps per checkpoint  [default: 4]"
    ),
    max_attempts: int = typer.Option(
        None,
        "--max-attempts",
        min=1,
        max=20,
        help="Submissions before giving up (recoverable failures only)  [default: 3]",
    ),
    elastic_restarts: int = typer.Option(
        None, "--elastic-restarts", min=0, help="torchrun in-job --max-restarts  [default: 0]"
    ),
    backoff: float = typer.Option(None, "--backoff", min=0.0, help="Base seconds between attempts"),
    seed: int = typer.Option(None, "--seed", help="Seed (default: EXAMLOPS_SEED, else 0)"),
    run_id: str = typer.Option(None, "--run-id", help="Explicit run id"),
    checkpoint_store: str = typer.Option(
        None,
        "--checkpoint-store",
        help="Durable checkpoint store: s3://bucket/prefix (MinIO) or a shared NFS mount  "
        "[default: EXAMLOPS_DIST_CHECKPOINT_STORE]",
    ),
    dataset_revision: str = typer.Option(
        None, "--dataset-revision", help="A1 dataset revision the run is pinned to (--scheduler)"
    ),
    mlflow_run_id: str = typer.Option(
        None, "--mlflow-run-id", help="MLflow run to link the checkpoints to (--scheduler)"
    ),
) -> None:
    """Train under real torchrun (locally or via the scheduler); resubmit and resume on failure."""
    import time

    from examlops.distributed.durable import DurableStoreUnavailable
    from examlops.distributed.launch import TorchNotInstalled, default_run_dir, supervise

    if local == scheduler:
        _output.error("Choose exactly one of --local or --scheduler.", exit_code=2)
    if strategy is not None and strategy not in _RUN_STRATEGIES:
        _output.error(f"--strategy must be one of {', '.join(_RUN_STRATEGIES)}", exit_code=2)
    if scheduler:
        if not model:
            _output.error("--scheduler needs --model (its YAML supplies the plan).", exit_code=2)
        # The same budget gate `exa pipeline run` consults (ADR 0029): this path submits real
        # scheduler jobs, so it must not be the way around an exhausted project budget.
        from examlops.cli.commands.pipeline import _enforce_budget_gate

        _enforce_budget_gate(None, model)
        run_scheduled(
            model,
            run_id=run_id,
            overrides={
                "strategy": strategy,
                "nodes": nodes,
                "min_nodes": min_nodes,
                "nproc_per_node": nproc,
                "steps": steps,
                "checkpoint_every": checkpoint_every,
                "max_attempts": max_attempts,
                "max_restarts": elastic_restarts,
            },
            seed=seed,
            checkpoint_store=checkpoint_store,
            dataset_revision=dataset_revision,
            mlflow_run_id=mlflow_run_id,
            backoff=backoff,
        )
        return
    if (nodes or 1) > 1 or min_nodes is not None:
        _output.error(
            "--local runs one node; use --scheduler for --nodes/--min-nodes.", exit_code=2
        )
    entrypoint = None
    if model:
        from examlops.distributed.strategy import (
            StrategyUnavailable,
            require_runnable,
            resolve_plan,
        )

        try:
            plan = resolve_plan(model, overrides={"strategy": strategy})
            require_runnable(plan)
        except (ValueError, StrategyUnavailable) as exc:
            _output.error(str(exc), exit_code=2)
        strategy, entrypoint = plan.strategy, plan.entrypoint
    elif strategy not in (None, "ddp", "fsdp"):
        _output.error(
            f"--strategy {strategy} needs a model entrypoint (--model with distributed.entrypoint)",
            exit_code=2,
        )
    rid = run_id or f"dist-ref-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    try:
        # The run id names a directory under the data root (and a durable-store key): the same
        # rule the scheduled path applies, so `--run-id ../x` cannot place the run elsewhere.
        from examlops.distributed.durable import _safe_run_id

        _safe_run_id(rid)
        res = supervise(
            rid,
            default_run_dir(rid),
            model=model or "reference-ddp",
            nproc_per_node=nproc or 2,
            steps=steps or 12,
            checkpoint_every=checkpoint_every or 4,
            seed=seed,
            max_attempts=max_attempts or 3,
            max_restarts=elastic_restarts or 0,
            backoff_s=1.0 if backoff is None else backoff,
            actor=_actor(),
            strategy=strategy or "ddp",
            entrypoint=entrypoint,
            checkpoint_store=checkpoint_store,
        )
    except TorchNotInstalled as exc:
        _output.error(str(exc), exit_code=2)
    except (DurableStoreUnavailable, ValueError) as exc:
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


# ADR 0109: the suspend/resume seam lives beside the checkpoints it pins.
from examlops.cli.commands import suspend_cmd as _suspend_cmd  # noqa: E402

app.add_typer(
    _suspend_cmd.app, name="suspend", help="Suspend/resume seam — pin, restore, release (ADR 0109)"
)
