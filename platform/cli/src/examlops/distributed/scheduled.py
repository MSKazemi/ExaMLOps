"""Distributed training through the phase-23 scheduler abstraction (ADR 0032 decisions 1-5).

``supervise_scheduled`` is the platform path ``exa pipeline run --distributed`` and
``exa pipeline distributed run --scheduler`` take. One attempt =

1. **restore** — if a durable checkpoint store is configured and holds a newer valid checkpoint
   than the run directory, it is downloaded and re-verified there (:mod:`.durable`);
2. **submit** — a generated ``run.sh`` goes to the configured scheduler (mock / Slurm / Flux) with
   the plan's topology as resources. Single node: ``torchrun --standalone``. Multi-node: ``srun``
   (Slurm) or ``flux run`` (Flux) starts one torchrun agent per node; the rendezvous host is the
   first node of the allocation, read **at run time** from the scheduler's node list
   (:mod:`.rendezvous`); ``min_nodes < nodes`` makes it torch-elastic (``--nnodes=min:max``,
   re-rendezvous on node loss, ``--max-restarts`` in-job);
3. **wait and classify** — the job's own completion line decides success; a FATAL marker decides
   "do not resubmit"; anything else is recoverable;
4. **mirror and record** — valid checkpoints are copied to the durable store, recorded in
   ``training_checkpoints`` (with their durable URI and MLflow run id) and the job in ``hpc_jobs``;
5. **resubmit** — a recoverable failure is resubmitted *through the scheduler* (bounded, with
   backoff, honouring the ADR 0109 preemption gate), and the new job resumes from the newest
   checkpoint that verifies.

On completion the run's resource use is recorded as cost (``model_costs`` → ``exa models cost``,
and ``distributed_runs.cost_gpu_hours``), the MLflow run (if given) is tagged with the checkpoint,
and an OpenLineage COMPLETE/FAIL event links dataset revision → run → model + checkpoint. Every
submission, failure, resume and completion is audited.

NCCL: the plan's ``nccl:`` settings (allow-listed ``NCCL_*``/``TORCH_NCCL_*`` names, restricted
values — configuration, never secrets) are exported in the job script, because an ``srun``-launched
rank on another node does not see the submitter's environment. The job also exports
``TORCH_NCCL_ASYNC_ERROR_HANDLING=1`` on GPU plans unless set, so a hung collective fails the
attempt instead of burning the allocation.

What remains unobserved (ADR 0032 Status): the multi-node / Slurm / Flux scripts are rendered and
unit-tested, and the mock scheduler runs the single-node script for real; no multi-node, NCCL or
GPU execution has been performed here.
"""

from __future__ import annotations

import datetime as _dt
import os
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from examlops import data as platform_db
from examlops.data.audit import audit_best_effort
from examlops.distributed import checkpoint_files as cf
from examlops.distributed import durable
from examlops.distributed import launch as _launch
from examlops.distributed.strategy import DistributedPlan, require_runnable

AUDIT_SOURCE = "exa-distributed"
RDZV_PORT_ENV = "EXAMLOPS_DIST_RDZV_PORT"
MAX_WAIT_ENV = "EXAMLOPS_DIST_MAX_WAIT_S"
_DEFAULT_RDZV_PORT = 29500
_ASYNC_ERR = "TORCH_NCCL_ASYNC_ERROR_HANDLING"
_MAX_ATTEMPTS_CAP = 20
_LOG_TAIL_BYTES = 256 * 1024


class SchedulerSubmitError(RuntimeError):
    """The scheduler refused the job (bad resources, queue closed): resubmitting would not help."""


@dataclass
class ScheduledAttempt:
    attempt: int
    job_id: str | None
    state: str
    exit_code: int | None
    outcome: str  # success | recoverable | fatal | cancelled
    seconds: float
    metrics: dict[str, Any] | None = None
    log: str = ""


@dataclass
class ScheduledRun:
    run_id: str
    run_dir: Path
    scheduler: str
    plan: dict[str, Any]
    status: str  # complete | failed | fatal | cancelled
    attempts: list[ScheduledAttempt] = field(default_factory=list)
    metrics: dict[str, Any] | None = None
    resumed_from_step: int | None = None
    restored_from_store: list[int] = field(default_factory=list)
    mirrored: list[int] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] | None = None
    mlflow_linked: bool = False
    preemption: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_dir"] = str(self.run_dir)
        return d


# ── script ─────────────────────────────────────────────────────────────────────


def _rdzv_port() -> int:
    raw = os.getenv(RDZV_PORT_ENV, "").strip()
    port = int(raw) if raw.isdigit() else _DEFAULT_RDZV_PORT
    if not 1024 <= port <= 65535:
        raise ValueError(f"{RDZV_PORT_ENV} must be 1024-65535, got {port}")
    return port


def job_env(plan: DistributedPlan, *, attempt: int, dataset_revision: str | None) -> dict[str, str]:
    """Variables the job script exports: attempt number, NCCL config, dataset pin. No secrets."""
    env = {"EXAMLOPS_DIST_ATTEMPT": str(attempt)}
    env.update(plan.nccl)  # validated by strategy.validate_nccl (names and values)
    if dataset_revision:
        env["EXAMLOPS_DATASET_REVISION"] = dataset_revision
    return env


def render_job_script(
    plan: DistributedPlan,
    run_id: str,
    run_dir: Path,
    *,
    scheduler: str,
    attempt: int = 1,
    seed: int | None = None,
    python: str | None = None,
    dataset_revision: str | None = None,
) -> str:
    """The ``run.sh`` for one attempt. Every interpolated value is shell-quoted."""
    from examlops import scheduler_jobs

    py = python or scheduler_jobs.job_python()
    multi = plan.nodes > 1 or plan.elastic
    if multi and scheduler == "mock":
        raise SchedulerSubmitError(
            "the mock scheduler runs one node; a multi-node plan needs EXAMLOPS_HPC_SCHEDULER="
            "slurm or flux"
        )
    if multi and scheduler not in ("slurm", "flux"):
        raise SchedulerSubmitError(f"no multi-node launcher for scheduler {scheduler!r}")
    placeholder = "__EXAMLOPS_RDZV__"
    cmd = _launch.build_torchrun_command(
        Path(run_dir),
        nproc_per_node=plan.nproc_per_node,
        steps=plan.steps,
        checkpoint_every=plan.checkpoint_every,
        seed=seed,
        max_restarts=plan.max_restarts,
        nnodes=plan.nodes,
        min_nodes=plan.min_nodes,
        rdzv_endpoint=placeholder if multi else None,
        rdzv_id=run_id,
        strategy=plan.strategy,
        entrypoint=plan.entrypoint,
        launcher=[py, "-m", "torch.distributed.run"],
    )
    quoted = " ".join(
        f'"--rdzv_endpoint=${{HEAD}}:{_rdzv_port()}"'
        if a == f"--rdzv_endpoint={placeholder}"
        else shlex.quote(str(a))
        for a in cmd
    )
    title = " ".join(f"distributed {run_id} attempt {attempt}".split())
    lines = [
        "#!/usr/bin/env bash",
        f"# ExaMLOps scheduler job: {title}. Generated — see examlops.distributed.scheduled.",
        "set -euo pipefail",
    ]
    roots = scheduler_jobs.pythonpath_roots(py)
    if roots:
        lines.append(
            f'export PYTHONPATH={shlex.quote(os.pathsep.join(roots))}"${{PYTHONPATH:+:$PYTHONPATH}}"'
        )
    for k, v in job_env(plan, attempt=attempt, dataset_revision=dataset_revision).items():
        lines.append(f"export {k}={shlex.quote(v)}")
    if plan.gpus_per_node > 0 and _ASYNC_ERR not in plan.nccl:
        # A default, not an override: a site that sets it in the job environment keeps its value.
        lines.append(f'export {_ASYNC_ERR}="${{{_ASYNC_ERR}:-1}}"')
    qpy = shlex.quote(py)
    if not multi:
        lines.append(f"exec {quoted}")
    elif scheduler == "slurm":
        lines.append(
            f'HEAD="$({qpy} -m examlops.distributed.rendezvous "${{SLURM_JOB_NODELIST:?}}")"'
        )
        lines.append(
            f"exec srun --nodes={plan.nodes} --ntasks-per-node=1 --kill-on-bad-exit=1 {quoted}"
        )
    else:  # flux
        lines.append(
            f'HEAD="$({qpy} -m examlops.distributed.rendezvous "$(flux getattr hostlist)")"'
        )
        lines.append(f"exec flux run -N {plan.nodes} -n {plan.nodes} {quoted}")
    return "\n".join(lines) + "\n"


def job_resources(plan: DistributedPlan, run_id: str) -> dict[str, Any]:
    """Scheduler-neutral resources (phase 23) for the plan: one task per node."""
    res: dict[str, Any] = {
        "nodes": plan.nodes,
        "ntasks": plan.nodes,
        "job_name": f"exa-dist-{run_id}"[:64],
    }
    if plan.gpus_per_node > 0:
        res["gpus_per_node"] = plan.gpus_per_node
        res["gpus"] = plan.gpus_per_node * plan.nodes
    return res


# ── cost / lineage / MLflow ────────────────────────────────────────────────────


def _iso_seconds(start: Any, end: Any) -> float | None:
    try:
        a = _dt.datetime.fromisoformat(str(start))
        b = _dt.datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    secs = (b - a).total_seconds()
    return secs if secs >= 0 else None


def run_cost(plan: DistributedPlan, seconds: float) -> dict[str, Any]:
    """Declared resources × wall clock (every attempt counts: a failed allocation was paid for)."""
    hours = max(0.0, seconds) / 3600.0
    gpu_h = hours * plan.nodes * plan.gpus_per_node
    cpu_h = 0.0 if plan.gpus_per_node else hours * plan.nodes * plan.nproc_per_node
    return {
        "metered": False,
        "basis": "declared nodes x (gpus|procs) per node x attempt wall clock",
        "seconds": round(seconds, 3),
        "gpu_hours": round(gpu_h, 6),
        "cpu_hours": round(cpu_h, 6),
    }


def record_cost(
    plan: DistributedPlan, run_id: str, cost: dict[str, Any], *, job_id: str | None
) -> dict[str, Any]:
    """Write the cost to ``distributed_runs`` and ``model_costs`` (``exa models cost``)."""
    out = dict(cost)
    try:
        from examlops.data.finops import record_model_cost
        from examlops.finops.cost import estimate_cost_via_provider

        usd = estimate_cost_via_provider(cost["gpu_hours"], cost["cpu_hours"]).get("cost_usd")
        # version 0 = "trained, not yet registered": the run precedes a registry version.
        record_model_cost(
            plan.model,
            0,
            run_id,
            job_id,
            cost["gpu_hours"],
            usd,
            cpu_hours=cost["cpu_hours"],
        )
        out["cost_usd"] = usd
        out["recorded"] = True
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo a finished run
        out["recorded"] = False
        out["error"] = str(exc)[:300]
    platform_db.update_distributed_run(run_id, cost_gpu_hours=cost["gpu_hours"])
    return out


MlflowTagger = Callable[[str, dict[str, str]], None]


def _mlflow_tagger(run_id: str, tags: dict[str, str]) -> None:
    import mlflow  # lazily: the CLI works without MLflow

    client = mlflow.tracking.MlflowClient()
    for k, v in tags.items():
        client.set_tag(run_id, k, v)


def link_mlflow(
    mlflow_run_id: str | None,
    run: ScheduledRun,
    *,
    tagger: MlflowTagger | None = None,
) -> bool:
    """Tag the MLflow run with the distributed run and its newest checkpoint. Best effort."""
    if not mlflow_run_id or not run.checkpoints:
        return False
    newest = run.checkpoints[-1]
    tags = {
        "examlops.distributed.run_id": run.run_id,
        "examlops.distributed.strategy": str(run.plan.get("strategy")),
        "examlops.distributed.world_size": str(newest.get("world_size")),
        "examlops.checkpoint.step": str(newest.get("step")),
        "examlops.checkpoint.uri": str(newest.get("uri") or newest.get("dir")),
    }
    if run.attempts and run.attempts[-1].job_id:
        tags["hpc_job_id"] = str(run.attempts[-1].job_id)
    try:
        (tagger or _mlflow_tagger)(mlflow_run_id, tags)
        return True
    except Exception as exc:  # noqa: BLE001 - MLflow down must not fail a finished run
        audit_best_effort(
            AUDIT_SOURCE, None, "mlflow_link_failed", run.run_id, {"error": str(exc)[:300]}
        )
        return False


def _lineage(
    event: str,
    run: ScheduledRun,
    plan: DistributedPlan,
    *,
    dataset_revision: str | None,
    mlflow_run_id: str | None,
) -> None:
    try:
        from examlops.lineage import Node, emit_lineage

        inputs = [Node(name=dataset_revision, type="dataset")] if dataset_revision else []
        outputs = [Node(name=plan.model, type="model")]
        if run.checkpoints:
            newest = run.checkpoints[-1]
            outputs.append(
                Node(name=str(newest.get("uri") or newest.get("dir")), type="checkpoint")
            )
        job = run.attempts[-1].job_id if run.attempts else None
        emit_lineage(
            event,
            job=f"distributed:{plan.model}",
            run_id=run.run_id,
            inputs=inputs,
            outputs=outputs,
            dataset_revision=dataset_revision,
            mlflow_run_id=mlflow_run_id,
            model=plan.model,
            hpc_job_id=job,
            scheduler=run.scheduler,
        )
    except Exception:  # noqa: BLE001 - lineage is fail-open by contract
        pass


# ── one attempt ────────────────────────────────────────────────────────────────


#: Someone other than the supervisor ended the job (``scancel`` / ``flux cancel``; sacct writes
#: ``CANCELLED by <uid>``). Resubmitting would fight the operator, so the run stops. Preemption is
#: not this: Slurm reports it as ``PREEMPTED``, and the supervisor's own lost-job cancel is
#: recorded as ``TIMEOUT``.
_OPERATOR_STOP_STATES = frozenset({"CANCELLED", "CANCELED", "REVOKED"})


def _norm_state(state: Any) -> str:
    text = str(state or "").strip().upper()
    return text.split()[0] if text else "UNKNOWN"


def _classify(
    state: str, metrics: dict[str, Any] | None, exit_code: int | None, run_dir: Path
) -> str:
    key = _norm_state(state)
    if key == "COMPLETED" and metrics and metrics.get("status") == "complete":
        return "success"
    if exit_code == cf.EXIT_FATAL or (Path(run_dir) / cf.FATAL_MARKER).exists():
        return "fatal"
    if key in _OPERATOR_STOP_STATES:
        return "cancelled"
    return "recoverable"


def _max_wait_seconds() -> int:
    raw = (os.getenv(MAX_WAIT_ENV) or "").strip()
    if not raw:
        return 86400
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{MAX_WAIT_ENV} must be a positive number of seconds, got {raw!r}")
    return int(raw)


def _cancel_quietly(adapter: Any, job_id: str | None) -> None:
    """Make sure a failed attempt's job is gone before its replacement is submitted.

    A scheduler that requeues on its own (Slurm ``--requeue`` / ``JobRequeue=1``) could otherwise
    run the old job again next to the resubmission, both writing the same run directory. Cancelling
    a job that has already ended is a no-op, so any error is ignored.
    """
    cancel = getattr(adapter, "cancel_job", None)
    if job_id and callable(cancel):
        try:
            cancel(job_id)
        except Exception:  # noqa: BLE001 - the job has usually ended already
            pass


def _run_attempt(
    adapter: Any,
    plan: DistributedPlan,
    run_id: str,
    run_dir: Path,
    *,
    scheduler: str,
    attempt: int,
    seed: int | None,
    dataset_revision: str | None,
    max_wait_s: int,
    poll_interval: int,
) -> ScheduledAttempt:
    from examlops import scheduler_jobs

    text = render_job_script(
        plan,
        run_id,
        run_dir,
        scheduler=scheduler,
        attempt=attempt,
        seed=seed,
        dataset_revision=dataset_revision,
    )
    script, job_key = scheduler_jobs.write_script("distributed", text)
    resources = job_resources(plan, run_id)
    t0 = time.monotonic()
    try:
        job_id = scheduler_jobs.submit(adapter, script, job_key, resources)
    except Exception as exc:  # noqa: BLE001 - adapter-specific submission errors
        raise SchedulerSubmitError(f"{scheduler} refused the job: {exc}") from exc
    scheduler_jobs.record_job(job_id, scheduler, f"distributed:{plan.model}", resources)
    audit_best_effort(
        AUDIT_SOURCE,
        None,
        "distributed_submit",
        run_id,
        {"attempt": attempt, "job_id": job_id, "scheduler": scheduler, "resources": resources},
    )
    timed_out = False
    try:
        adapter.wait_until_complete(job_id, poll_interval=poll_interval, max_wait_s=max_wait_s)
    except Exception as exc:  # noqa: BLE001 - JobTimeoutError and friends: the job is lost
        timed_out = True
        cancel = getattr(adapter, "cancel_job", None)
        if callable(cancel):
            try:
                cancel(job_id)
            except Exception:  # noqa: BLE001
                pass
        audit_best_effort(
            AUDIT_SOURCE,
            None,
            "distributed_job_lost",
            run_id,
            {"attempt": attempt, "job_id": job_id, "error": str(exc)[:300]},
        )
    wall = time.monotonic() - t0
    try:
        status = dict(adapter.get_job_status(job_id))
    except Exception:  # noqa: BLE001
        status = {"state": "UNKNOWN", "exit_code": None}
    if timed_out:
        status["state"] = "TIMEOUT"
    scheduler_jobs.finish_job(job_id, scheduler, status)
    try:
        logs = str(adapter.get_job_logs(job_id) or "")
    except Exception:  # noqa: BLE001
        logs = ""
    logs = logs[-_LOG_TAIL_BYTES:]
    log_path = Path(run_dir) / f"attempt-{attempt}.log"
    log_path.write_text(logs)
    metrics = cf.parse_metrics(logs)
    state = str(status.get("state") or "UNKNOWN")
    exit_code = status.get("exit_code")
    secs = _iso_seconds(status.get("start_time"), status.get("end_time"))
    return ScheduledAttempt(
        attempt=attempt,
        job_id=job_id,
        state=state,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        outcome=_classify(
            state, metrics, exit_code if isinstance(exit_code, int) else None, run_dir
        ),
        seconds=round(secs if secs is not None else wall, 3),
        metrics=metrics,
        log=str(log_path),
    )


# ── supervisor ─────────────────────────────────────────────────────────────────


def supervise_scheduled(
    plan: DistributedPlan,
    run_id: str,
    run_dir: Path | None = None,
    *,
    adapter: Any | None = None,
    scheduler: str | None = None,
    seed: int | None = None,
    checkpoint_store: str | None = None,
    dataset_revision: str | None = None,
    mlflow_run_id: str | None = None,
    backoff_s: float = 5.0,
    backoff_cap_s: float = 300.0,
    max_wait_s: int | None = None,
    poll_interval: int = 10,
    actor: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    mlflow_tagger: MlflowTagger | None = None,
    skip_preflight: bool = False,
) -> ScheduledRun:
    """Submit ``plan`` through the scheduler; resubmit a recoverable failure; record the outcome.

    Raises :class:`~examlops.distributed.strategy.StrategyUnavailable` (preflight),
    :class:`~examlops.distributed.durable.DurableStoreUnavailable` (store configured but unusable)
    or ``ValueError`` (bad run id / plan) before anything is submitted.
    """
    from examlops import scheduler_jobs

    if not skip_preflight:
        require_runnable(plan)
    durable._safe_run_id(run_id)
    if not 1 <= plan.max_attempts <= _MAX_ATTEMPTS_CAP:
        raise ValueError(f"max_attempts must be 1-{_MAX_ATTEMPTS_CAP}, got {plan.max_attempts}")
    sched = (scheduler or scheduler_jobs.scheduler_name()).strip().lower()
    store = durable.resolve_store(checkpoint_store)
    run_dir = Path(run_dir) if run_dir is not None else _launch.default_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / cf.FATAL_MARKER).unlink(missing_ok=True)
    wait = max_wait_s if max_wait_s is not None else _max_wait_seconds()
    adapter = adapter if adapter is not None else scheduler_jobs.scheduler_adapter()

    result = ScheduledRun(
        run_id=run_id, run_dir=run_dir, scheduler=sched, plan=plan.to_dict(), status="failed"
    )
    platform_db.create_distributed_run(
        run_id,
        plan.model,
        nodes=plan.nodes,
        gpus_per_node=plan.gpus_per_node,
        strategy=plan.strategy,
        dataset_revision=dataset_revision,
        checkpoint_every=f"{plan.checkpoint_every} steps",
    )
    audit_best_effort(
        AUDIT_SOURCE,
        actor,
        "distributed_launch",
        run_id,
        {
            "model": plan.model,
            "scheduler": sched,
            "plan": plan.to_dict(),
            "checkpoint_store": store.url if store else None,
            "dataset_revision": dataset_revision,
        },
    )
    _lineage("START", result, plan, dataset_revision=dataset_revision, mlflow_run_id=mlflow_run_id)

    total_seconds = 0.0
    last_job: str | None = None
    durable_steps: set[int] = set()
    for n in range(1, plan.max_attempts + 1):
        if store is not None:
            restored = durable.restore_latest(store, run_id, run_dir, actor=actor)
            if restored is not None:
                result.restored_from_store.append(restored)
        try:
            att = _run_attempt(
                adapter,
                plan,
                run_id,
                run_dir,
                scheduler=sched,
                attempt=n,
                seed=seed,
                dataset_revision=dataset_revision,
                max_wait_s=wait,
                poll_interval=poll_interval,
            )
        except SchedulerSubmitError as exc:
            result.status, result.error = "fatal", str(exc)
            audit_best_effort(
                AUDIT_SOURCE, actor, "distributed_submit_failed", run_id, {"error": str(exc)[:500]}
            )
            break
        result.attempts.append(att)
        total_seconds += att.seconds
        last_job = att.job_id
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "distributed_attempt",
            run_id,
            {
                "attempt": n,
                "job_id": att.job_id,
                "state": att.state,
                "outcome": att.outcome,
                "seconds": att.seconds,
            },
        )
        cfg_hash = (att.metrics or {}).get("config_hash")
        if store is not None:
            mres = durable.mirror_run(store, run_id, run_dir, cfg_hash, actor=actor)
            result.mirrored += mres.mirrored
            durable_steps.update(mres.mirrored + mres.already)
        result.checkpoints = _launch._register_checkpoints(
            run_id,
            run_dir,
            cfg_hash,
            uri_for=durable.uri_for(store, run_id, only=durable_steps),
            mlflow_run_id=mlflow_run_id,
        )
        if att.outcome == "success":
            result.status, result.metrics = "complete", att.metrics
            break
        if att.outcome == "fatal":
            result.status = "fatal"
            break
        if att.outcome == "cancelled":
            result.status = "cancelled"
            result.error = f"job {att.job_id} was cancelled outside the supervisor ({att.state})"
            break
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "distributed_failed",
            run_id,
            {"reason": f"job {att.job_id} ended {att.state}", "attempt": n},
        )
        platform_db.update_distributed_run(run_id, status="failed")
        if n < plan.max_attempts:
            if _launch.preemption_gate_enabled():
                promised, reasons = _launch.preemption_check(run_id, run_dir, cfg_hash)
                result.preemption = {"gate": "on", "promised": promised, "reasons": list(reasons)}
                if not promised:
                    audit_best_effort(
                        AUDIT_SOURCE,
                        actor,
                        "distributed_resubmit_declined",
                        run_id,
                        {"attempt": n, "reasons": list(reasons)},
                    )
                    break
            if att.state != "TIMEOUT":  # a lost job was already cancelled in _run_attempt
                _cancel_quietly(adapter, att.job_id)
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "distributed_resubmit",
                run_id,
                {"attempt": n + 1, "scheduler": sched},
            )
            sleep(min(backoff_cap_s, backoff_s * (2 ** (n - 1))))

    claimed = (result.metrics or {}).get("resumed_from_step")
    if claimed is not None and cf.verify_checkpoint_dir(cf.step_dir(run_dir, int(claimed))).valid:
        result.resumed_from_step = int(claimed)
        platform_db.update_distributed_run(run_id, status="resumed", bump_resumes=True)
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "distributed_resume",
            run_id,
            {"step": result.resumed_from_step, "evidence": "run metrics + manifest"},
        )
    if result.attempts:
        result.cost = record_cost(plan, run_id, run_cost(plan, total_seconds), job_id=last_job)
    if result.status == "complete":
        platform_db.update_distributed_run(run_id, status="complete")
        result.mlflow_linked = link_mlflow(mlflow_run_id, result, tagger=mlflow_tagger)
        audit_best_effort(
            AUDIT_SOURCE, actor, "distributed_complete", run_id, {"cost": result.cost}
        )
        _lineage(
            "COMPLETE", result, plan, dataset_revision=dataset_revision, mlflow_run_id=mlflow_run_id
        )
    else:
        platform_db.update_distributed_run(run_id, status="failed")
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            {
                "fatal": "distributed_fatal",
                "cancelled": "distributed_cancelled",
            }.get(result.status, "distributed_gave_up"),
            run_id,
            {"attempts": len(result.attempts), "error": result.error},
        )
        _lineage(
            "FAIL", result, plan, dataset_revision=dataset_revision, mlflow_run_id=mlflow_run_id
        )
    return result


__all__ = [
    "ScheduledAttempt",
    "ScheduledRun",
    "SchedulerSubmitError",
    "job_env",
    "job_resources",
    "link_mlflow",
    "record_cost",
    "render_job_script",
    "run_cost",
    "supervise_scheduled",
]
