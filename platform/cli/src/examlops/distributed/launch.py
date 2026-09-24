"""Launch and supervise real distributed training (ADR 0032 decisions 1-3, single node).

``build_torchrun_command`` builds the actual ``torchrun`` invocation of the reference script
(``train_ddp``); ``supervise`` runs it, and on a recoverable failure resubmits it — bounded, with
backoff — so the script resumes from the newest checkpoint that verifies. Every attempt is audited
and the run is recorded in ``distributed_runs`` / ``training_checkpoints``.

**A resume is only ever reported from evidence the run itself wrote**: the ``resumed_from_step`` in
its final metrics line, cross-checked against a valid manifest for that step on disk. Nothing here
infers "resumed" from "this was attempt 2".

``EXAMLOPS_SUSPEND_PREEMPTION_GATE`` (default off) makes the supervisor the ADR 0109 decision-3
consumer: before it spends another submission it asks the suspend seam whether the run's state
actually survives, and declines — audibly, with reasons — instead of restarting from step 0 while
calling it a resume. With the switch off, nothing about this module changes.

This module imports no torch: ``examlops.cli`` stays importable without it, and running a job
without torch installed gives :class:`TorchNotInstalled` (a clear message), not an ImportError at
CLI start-up. Only the worker processes need torch.

What this does **not** do (see ADR 0032's Status): submit through a scheduler, multi-node rendezvous
(the builder emits the flags, nothing has run them across nodes), NCCL/GPU (the script selects it,
nothing has run it), FSDP/DeepSpeed, or MinIO/NFS shard storage.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import examlops
from examlops import data as platform_db
from examlops.data.audit import audit_best_effort
from examlops.distributed import checkpoint_files as cf
from examlops.distributed import train_ddp

AUDIT_SOURCE = "exa-distributed"
STRATEGY = "ddp"


class TorchNotInstalled(RuntimeError):
    """PyTorch is required to run distributed training and is not importable here."""


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def default_run_dir(run_id: str) -> Path:
    """``$EXAMLOPS_DATA_DIR/distributed/<run>``, else ``$XDG_DATA_HOME/examlops/distributed/<run>``."""
    from examlops.lifecycle import datadir

    root = datadir.data_path("distributed")
    if root is None:
        xdg = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        root = Path(xdg) / "examlops" / "distributed"
    return root / run_id


def _launcher_prefix() -> list[str]:
    """``torchrun`` from this interpreter's environment; ``python -m torch.distributed.run`` else."""
    exe = Path(sys.executable).parent / "torchrun"
    if exe.exists():
        return [str(exe)]
    found = shutil.which("torchrun")
    return [found] if found else [sys.executable, "-m", "torch.distributed.run"]


def build_torchrun_command(
    run_dir: Path,
    *,
    nproc_per_node: int = 2,
    steps: int = 12,
    checkpoint_every: int = 4,
    seed: int | None = None,
    max_restarts: int = 0,
    nnodes: int = 1,
    rdzv_endpoint: str | None = None,
    rdzv_id: str | None = None,
) -> list[str]:
    """The real torchrun command for the reference script.

    ``nnodes == 1`` uses ``--standalone`` (a local c10d rendezvous on a free port).
    ``nnodes > 1`` emits the c10d flags against ``rdzv_endpoint``; those flags are built and
    unit-tested as a string only — no multi-node run has been performed.
    ``max_restarts`` is torchrun's in-job elastic restart budget: workers are restarted inside the
    same job and (because the script resumes from its checkpoints) continue from the last valid one.
    """
    if nproc_per_node < 1 or nnodes < 1 or max_restarts < 0:
        raise ValueError("nproc_per_node and nnodes must be >= 1 and max_restarts >= 0")
    cmd = [*_launcher_prefix()]
    if nnodes == 1:
        cmd.append("--standalone")
    else:
        if not rdzv_endpoint:
            raise ValueError("a multi-node launch needs rdzv_endpoint")
        cmd += [
            f"--nnodes={nnodes}",
            "--rdzv_backend=c10d",
            f"--rdzv_endpoint={rdzv_endpoint}",
            f"--rdzv_id={rdzv_id or 'examlops'}",
        ]
    cmd += [
        f"--nproc-per-node={nproc_per_node}",
        f"--max-restarts={max_restarts}",
        str(Path(train_ddp.__file__).resolve()),
        f"--run-dir={run_dir}",
        f"--steps={steps}",
        f"--checkpoint-every={checkpoint_every}",
    ]
    if seed is not None:
        cmd.append(f"--seed={seed}")
    return cmd


@dataclass
class Attempt:
    attempt: int
    returncode: int
    outcome: str  # success | recoverable | fatal
    seconds: float
    metrics: dict[str, Any] | None = None
    log: str = ""


@dataclass
class SupervisedRun:
    run_id: str
    run_dir: Path
    status: str  # complete | failed | fatal
    attempts: list[Attempt] = field(default_factory=list)
    metrics: dict[str, Any] | None = None
    resumed_from_step: int | None = None  # from the run's own manifest; None = never resumed
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    # Set only when the preemption gate ran (it is off unless EXAMLOPS_SUSPEND_PREEMPTION_GATE is
    # on); None therefore means "the gate was not consulted", never "it said yes".
    preemption: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_dir"] = str(self.run_dir)
        return d


Runner = Callable[[list[str], dict[str, str], Path, float], int]

#: Kill-switch for the ADR 0109 decision-3 consumer below. Off ⇒ ``supervise`` behaves exactly as
#: it did before the gate existed.
PREEMPTION_GATE_ENV = "EXAMLOPS_SUSPEND_PREEMPTION_GATE"


def preemption_gate_enabled() -> bool:
    return os.getenv(PREEMPTION_GATE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def preemption_check(
    run_id: str,
    run_dir: Path,
    cfg_hash: str | None = None,
    *,
    backend: str = "training-checkpoint",
) -> tuple[bool, tuple[str, ...]]:
    """Ask the suspend seam whether resubmitting this run preserves its state (ADR 0109 dec. 3).

    This is the consumer decision 3 asks for: *"if the backend cannot support checkpoint-preserving
    preemption, the broker declines to promise it and states why — it does not silently kill and
    restart."* The supervisor is the platform's only code path that actually re-runs a workload
    after a preemption-shaped failure, so a declined promise has something real to change here.

    Two independent questions, both of which must answer yes:

    1. *the mechanism* — ``preemption_promise(capability)``: does the backend keep state in a tier
       that outlives the compute at all?
    2. *this run* — does a checkpoint that verifies actually exist on disk? A backend that could
       preserve state does not help a run that has not written any, and resubmitting such a run is
       a restart from step 0 wearing the word "resume".

    Returns ``(can_promise, reasons)``; ``reasons`` carries the caveats even when it says yes, so
    the ceiling stays visible (the same convention as ``PreemptionPromise``).
    """
    from examlops.suspend import preemption_promise
    from examlops.suspend import service as suspend_service

    try:
        capability = suspend_service.get_backend(backend).capability()
    except Exception as exc:  # noqa: BLE001 - an unresolvable backend promises nothing
        return False, (f"suspend backend {backend!r} is unavailable: {exc}",)
    promise = preemption_promise(capability)
    reasons = [f"backend {capability.backend!r}", *promise.reasons]
    latest, _skipped = cf.find_latest_valid(run_dir, cfg_hash)
    if latest is None:
        reasons.append(
            f"run {run_id!r} has no checkpoint that verifies: a resubmission would restart from "
            "step 0, not resume"
        )
        return False, tuple(reasons)
    reasons.append(f"newest valid checkpoint: step {latest.step}")
    return promise.can_promise, tuple(reasons)


def _subprocess_runner(cmd: list[str], env: dict[str, str], log: Path, timeout: float) -> int:
    with open(log, "wb") as fh:
        try:
            return subprocess.run(  # noqa: S603 - argv list built above, no shell
                cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            return 124


def classify(returncode: int, metrics: dict[str, Any] | None, run_dir: Path) -> str:
    """success only with rc 0 AND the script's own completion line; a FATAL marker beats a retry.

    ``torchrun`` reports any worker failure as its own exit status 1 whatever the worker exited
    with, so the marker the script wrote (not the return code) is what says "do not resubmit".
    """
    if returncode == cf.EXIT_OK and metrics and metrics.get("status") == "complete":
        return "success"
    if returncode == cf.EXIT_FATAL or (Path(run_dir) / cf.FATAL_MARKER).exists():
        return "fatal"
    return "recoverable"


def _register_checkpoints(run_id: str, run_dir: Path, cfg_hash: str | None) -> list[dict[str, Any]]:
    """Record each valid on-disk checkpoint once in ``training_checkpoints`` (state = manifest)."""
    from examlops.distributed import write_checkpoint

    have = {c["step"] for c in platform_db.list_training_checkpoints(run_id)}
    out = []
    for st in reversed(cf.list_checkpoints(run_dir, cfg_hash)):  # oldest first
        if not st.valid:
            continue
        m = st.manifest
        if st.step not in have:
            write_checkpoint(
                run_id,
                st.step,
                int(m.get("epoch", 0)),
                {
                    "manifest_sha256": m["manifest_sha256"],
                    "world_size": m["world_size"],
                    "config_hash": m["config_hash"],
                    "shards": [{k: s[k] for k in ("rank", "sha256")} for s in m["shards"]],
                },
                shard_count=int(m["world_size"]),
                uri=str(st.directory),
            )
        out.append({"step": st.step, "world_size": m["world_size"], "dir": str(st.directory)})
    return out


def supervise(
    run_id: str,
    run_dir: Path,
    *,
    model: str = "reference-ddp",
    nproc_per_node: int = 2,
    steps: int = 12,
    checkpoint_every: int = 4,
    seed: int | None = None,
    max_attempts: int = 3,
    max_restarts: int = 0,
    backoff_s: float = 1.0,
    backoff_cap_s: float = 30.0,
    timeout_s: float = 600.0,
    env_extra: dict[str, str] | None = None,
    actor: str | None = None,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    require_torch: bool = True,
) -> SupervisedRun:
    """Run, and on a recoverable failure resubmit, up to ``max_attempts`` times."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if runner is None and require_torch and not torch_available():
        raise TorchNotInstalled(
            "torch is not installed in this environment; distributed training needs it "
            "(pip install torch). Nothing was launched."
        )
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / cf.FATAL_MARKER).unlink(missing_ok=True)
    run = runner or _subprocess_runner
    cmd = build_torchrun_command(
        run_dir,
        nproc_per_node=nproc_per_node,
        steps=steps,
        checkpoint_every=checkpoint_every,
        seed=seed,
        max_restarts=max_restarts,
    )
    platform_db.create_distributed_run(
        run_id,
        model,
        nodes=1,
        gpus_per_node=nproc_per_node,
        strategy=STRATEGY,
        checkpoint_every=f"{checkpoint_every} steps",
    )
    pkg_root = str(Path(examlops.__file__).resolve().parent.parent)
    result = SupervisedRun(run_id=run_id, run_dir=run_dir, status="failed")
    audit_best_effort(
        AUDIT_SOURCE,
        actor,
        "distributed_launch",
        run_id,
        {
            "model": model,
            "nproc": nproc_per_node,
            "steps": steps,
            "strategy": STRATEGY,
            "max_attempts": max_attempts,
        },
    )
    for n in range(1, max_attempts + 1):
        env = {**os.environ, **(env_extra or {})}
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [pkg_root, env.get("PYTHONPATH")]))
        env["EXAMLOPS_DIST_ATTEMPT"] = str(n)
        log = run_dir / f"attempt-{n}.log"
        t0 = time.monotonic()
        rc = run(cmd, env, log, timeout_s)
        secs = time.monotonic() - t0
        text = log.read_text(errors="replace") if log.exists() else ""
        metrics = cf.parse_metrics(text)
        outcome = classify(rc, metrics, run_dir)
        result.attempts.append(Attempt(n, rc, outcome, round(secs, 3), metrics, str(log)))
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "distributed_attempt",
            run_id,
            {"attempt": n, "returncode": rc, "outcome": outcome, "seconds": round(secs, 3)},
        )
        cfg_hash = (metrics or {}).get("config_hash")
        result.checkpoints = _register_checkpoints(run_id, run_dir, cfg_hash)
        if outcome == "success":
            result.status, result.metrics = "complete", metrics
            break
        if outcome == "fatal":
            result.status = "fatal"
            break
        # recoverable
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "distributed_failed",
            run_id,
            {"reason": f"attempt {n} exited {rc}", "attempt": n},
        )
        platform_db.update_distributed_run(run_id, status="failed")
        if n < max_attempts:
            # ADR 0109 decision 3. Off by default: the gate can only ever *stop* a resubmission
            # that would otherwise have happened, never start one.
            if preemption_gate_enabled():
                promised, reasons = preemption_check(run_id, run_dir, cfg_hash)
                result.preemption = {
                    "gate": "on",
                    "promised": promised,
                    "reasons": list(reasons),
                }
                if not promised:
                    audit_best_effort(
                        AUDIT_SOURCE,
                        actor,
                        "distributed_resubmit_declined",
                        run_id,
                        {"attempt": n, "reasons": list(reasons)},
                    )
                    break
            sleep(min(backoff_cap_s, backoff_s * (2 ** (n - 1))))

    # A resume is claimed only from the run's own metrics, and only if that step's manifest is valid.
    claimed = (result.metrics or {}).get("resumed_from_step")
    if claimed is not None:
        st = cf.verify_checkpoint_dir(cf.step_dir(run_dir, int(claimed)))
        if st.valid:
            result.resumed_from_step = int(claimed)
            platform_db.update_distributed_run(run_id, status="resumed", bump_resumes=True)
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "distributed_resume",
                run_id,
                {"step": result.resumed_from_step, "evidence": "run metrics + manifest"},
            )
    if result.status == "complete":
        platform_db.update_distributed_run(run_id, status="complete")
        audit_best_effort(AUDIT_SOURCE, actor, "distributed_complete", run_id, {})
    elif result.status == "fatal":
        platform_db.update_distributed_run(run_id, status="failed")
        audit_best_effort(AUDIT_SOURCE, actor, "distributed_fatal", run_id, {})
    return result
