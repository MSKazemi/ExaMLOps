"""Run the reference LoRA fine-tune and register the adapter it actually produced (ADR 0044).

The rule this module exists to enforce: **an adapter's ``eval_score`` is written only from a
training run's own held-out evaluation.** Nothing here accepts a score from a caller. An operator
who wants to record a number they obtained elsewhere writes it to a different column
(``asserted_eval_score``, via :func:`examlops.finetuning.finetune`), where it is labelled
unverified and can never be mistaken for a measurement.

Shape mirrors :mod:`examlops.distributed.launch`: build the argv for the shipped script, run it in
a subprocess, classify the outcome from the script's own exit code and completion line, audit
every attempt, and resume on a recoverable failure (the script picks up its last valid
checkpoint). What it does **not** do: submit to Slurm/Flux, train on a GPU, or fine-tune a real
base checkpoint — see the ADR's Status.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import examlops
from examlops.data.audit import audit_best_effort
from examlops.distributed import checkpoint_files as cf
from examlops.finetuning import train_lora

AUDIT_SOURCE = "exa-finetune"


class TorchNotInstalled(RuntimeError):
    """PyTorch is required to fine-tune and is not importable here."""


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def default_run_dir(run_id: str) -> Path:
    """``$EXAMLOPS_DATA_DIR/finetune/<run>``, else ``$XDG_DATA_HOME/examlops/finetune/<run>``."""
    from examlops.lifecycle import datadir

    root = datadir.data_path("finetune")
    if root is None:
        xdg = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        root = Path(xdg) / "examlops" / "finetune"
    return root / run_id


def build_command(
    run_dir: Path,
    *,
    steps: int = train_lora.DEFAULT_STEPS,
    checkpoint_every: int = 20,
    batch: int = train_lora.DEFAULT_BATCH,
    rank: int = 4,
    alpha: float = 16.0,
    lr: float = train_lora.DEFAULT_LR,
    method: str = "lora",
    backend: str | None = None,
    seed: int | None = None,
) -> list[str]:
    """The real command that runs the shipped script — no shell, every value a separate argv item."""
    from examlops.finetuning import lora

    cmd = [
        sys.executable,
        "-m",
        "examlops.finetuning.train_lora",
        f"--run-dir={run_dir}",
        f"--steps={steps}",
        f"--checkpoint-every={checkpoint_every}",
        f"--batch={batch}",
        f"--rank={rank}",
        f"--alpha={alpha}",
        f"--lr={lr}",
        f"--method={method}",
        f"--backend={backend or lora.DEFAULT_BACKEND}",
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


@dataclass
class FinetuneRun:
    run_id: str
    run_dir: Path
    status: str  # complete | failed | fatal
    attempts: list[Attempt] = field(default_factory=list)
    metrics: dict[str, Any] | None = None
    adapter_id: str | None = None
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["run_dir"] = str(self.run_dir)
        return d


Runner = Callable[[list[str], dict[str, str], Path, float], int]


def _subprocess_runner(cmd: list[str], env: dict[str, str], log: Path, timeout: float) -> int:
    with open(log, "wb") as fh:
        try:
            return subprocess.run(  # noqa: S603 - argv list built above, no shell
                cmd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            return 124


def classify(returncode: int, metrics: dict[str, Any] | None, run_dir: Path) -> str:
    """Success needs rc 0 *and* the script's own completion line; a FATAL marker beats a retry."""
    if returncode == cf.EXIT_OK and metrics and metrics.get("status") == "complete":
        return "success"
    if returncode == cf.EXIT_FATAL or (Path(run_dir) / cf.FATAL_MARKER).exists():
        return "fatal"
    return "recoverable"


def run_finetune(
    base: str,
    *,
    dataset_rev: str,
    adapter_id: str | None = None,
    run_id: str | None = None,
    run_dir: Path | None = None,
    method: str = "lora",
    rank: int = 4,
    alpha: float = 16.0,
    steps: int = train_lora.DEFAULT_STEPS,
    checkpoint_every: int = 20,
    batch: int = train_lora.DEFAULT_BATCH,
    lr: float = train_lora.DEFAULT_LR,
    backend: str | None = None,
    seed: int | None = None,
    eval_floor: float = 0.0,
    max_attempts: int = 2,
    timeout_s: float = 900.0,
    actor: str | None = None,
    runner: Runner | None = None,
    register: bool = True,
    require_torch: bool = True,
) -> FinetuneRun:
    """Fine-tune, then register the adapter **with the score the run measured**.

    Returns without registering anything when the run does not complete: an adapter row is
    evidence that training happened, so a failed run must not leave one.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if runner is None and require_torch and not torch_available():
        raise TorchNotInstalled(
            "torch is not installed in this environment; fine-tuning needs it "
            "(pip install torch). Nothing was launched."
        )
    rid = run_id or f"ft-{int(time.time())}"
    directory = Path(run_dir) if run_dir is not None else default_run_dir(rid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / cf.FATAL_MARKER).unlink(missing_ok=True)
    run = runner or _subprocess_runner
    cmd = build_command(
        directory,
        steps=steps,
        checkpoint_every=checkpoint_every,
        batch=batch,
        rank=rank,
        alpha=alpha,
        lr=lr,
        method=method,
        backend=backend,
        seed=seed,
    )
    pkg_root = str(Path(examlops.__file__).resolve().parent.parent)
    result = FinetuneRun(run_id=rid, run_dir=directory, status="failed")
    audit_best_effort(
        AUDIT_SOURCE,
        actor,
        "finetune_started",
        rid,
        {"base": base, "method": method, "rank": rank, "steps": steps, "dataset": dataset_rev},
    )
    for n in range(1, max_attempts + 1):
        env = {**os.environ}
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [pkg_root, env.get("PYTHONPATH")]))
        log = directory / f"attempt-{n}.log"
        t0 = time.monotonic()
        rc = run(cmd, env, log, timeout_s)
        secs = time.monotonic() - t0
        text = log.read_text(errors="replace") if log.exists() else ""
        metrics = train_lora.parse_metrics(text)
        outcome = classify(rc, metrics, directory)
        result.attempts.append(Attempt(n, rc, outcome, round(secs, 3)))
        result.log = str(log)
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "finetune_attempt",
            rid,
            {"attempt": n, "returncode": rc, "outcome": outcome, "seconds": round(secs, 3)},
        )
        if outcome == "success":
            result.status, result.metrics = "complete", metrics
            break
        if outcome == "fatal":
            result.status = "fatal"
            break
    if result.status != "complete":
        audit_best_effort(AUDIT_SOURCE, actor, "finetune_failed", rid, {"status": result.status})
        return result

    metrics = result.metrics or {}
    if register:
        from examlops.finetuning import register_measured_adapter

        adapter = register_measured_adapter(
            adapter_id or f"{base}-{method}-{dataset_rev}"[:120],
            base,
            method=method,
            rank=rank,
            dataset_revision=dataset_rev,
            eval_score=float(metrics["eval_score"]),
            eval_metric=str(metrics["eval_metric"]),
            eval_n=int(metrics["eval_n"]),
            eval_floor=eval_floor,
            train_run_id=rid,
            adapter_sha256=str(metrics["adapter_sha256"]),
            adapter_uri=str(directory),
            actor=actor,
        )
        result.adapter_id = adapter.adapter_id
    audit_best_effort(
        AUDIT_SOURCE,
        actor,
        "finetune_complete",
        rid,
        {
            "adapter_id": result.adapter_id,
            "eval_metric": metrics.get("eval_metric"),
            "eval_score": metrics.get("eval_score"),
            "baseline_eval_score": metrics.get("baseline_eval_score"),
            "steps": metrics.get("steps"),
        },
    )
    return result


__all__ = [
    "AUDIT_SOURCE",
    "Attempt",
    "FinetuneRun",
    "TorchNotInstalled",
    "build_command",
    "classify",
    "default_run_dir",
    "run_finetune",
    "torch_available",
]
