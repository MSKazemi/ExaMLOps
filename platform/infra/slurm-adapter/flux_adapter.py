"""
Flux scheduler adapter for ExaMLOps.

Submits training jobs to a Flux instance (flux-core) via ``flux batch`` and monitors them
with ``flux jobs`` / ``flux job info``. Commands run through a ``RemoteExecutor`` so the
same adapter works locally (worker on the Flux node) or over SSH (Docker worker → remote
login node such as lxp-cpu01, which runs flux-core with no shared filesystem).

Flux has no stable REST daemon, so CLI-over-SSH is the realistic integration path.

Resource mapping (scheduler-neutral dict → flux flags):
    nodes         → -N          ntasks        → -n
    cpus_per_task → -c          gpus          → -g (emitted only when > 0)
    time          → -t<seconds> (converted from HH:MM:SS)
    job_name      → --job-name  qos           → --queue
    account       → --bank      (flux-accounting only; skipped if unavailable)
    constraint    → --requires  mem           → dropped (flux-core has no schedulable mem)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from executor import RemoteExecutor
from scheduler import (
    _CMD_TIMEOUT,
    BasePollingAdapter,
    JobNotFoundError,
    JobStatus,
    JobSubmissionError,
    SchedulerAdapterError,
)

# Flux job state / result → normalized state (mapped onto scheduler._TERMINAL_STATES).
_ACTIVE_MAP = {
    "RUN": "RUNNING",
    "CLEANUP": "RUNNING",
    "DEPEND": "PENDING",
    "PRIORITY": "PENDING",
    "SCHED": "PENDING",
}
_RESULT_MAP = {
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELED": "CANCELLED",
    "CANCELLED": "CANCELLED",
    "TIMEOUT": "TIMEOUT",
}


def _to_fsd(time_str: str) -> str:
    """Convert a Slurm-style ``HH:MM:SS`` (or ``MM:SS``/plain seconds) to Flux seconds."""
    time_str = str(time_str).strip()
    if ":" not in time_str:
        # Already FSD (e.g. "30m", "2h") or a bare number of minutes — pass through.
        return time_str
    parts = [int(p) for p in time_str.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:  # pragma: no cover - defensive
        return time_str
    return f"{h * 3600 + m * 60 + s}s"


class FluxAdapter(BasePollingAdapter):
    """Real Flux adapter — runs ``flux`` over a RemoteExecutor."""

    def __init__(
        self,
        executor: RemoteExecutor,
        working_dir: str = "flux_jobs",
        remote_workdir: str | None = None,
    ):
        self.executor = executor
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        # Root of per-job dirs on the (possibly remote) cluster.
        import os  # noqa: PLC0415

        self._remote_workdir = (
            remote_workdir or os.getenv("EXAMLOPS_HPC_REMOTE_WORKDIR") or str(self.working_dir)
        )
        self._jobdir: dict[str, str] = {}

    # ── submission ───────────────────────────────────────────────────────────

    def _flux_flags(self, resources: dict) -> list[str]:
        flags: list[str] = []
        if resources.get("nodes"):
            flags.append(f"-N{resources['nodes']}")
        if resources.get("ntasks"):
            flags.append(f"-n{resources['ntasks']}")
        if resources.get("cpus_per_task"):
            flags.append(f"-c{resources['cpus_per_task']}")
        gpus = resources.get("gpus")
        if gpus is not None and str(gpus).isdigit() and int(gpus) > 0:
            flags.append(f"-g{gpus}")
        if resources.get("time"):
            flags.append(f"-t{_to_fsd(resources['time'])}")
        if resources.get("job_name"):
            flags.append(f"--job-name={resources['job_name']}")
        if resources.get("qos"):
            flags.append(f"--queue={resources['qos']}")
        if resources.get("account"):
            flags.append(f"--bank={resources['account']}")
        if resources.get("constraint"):
            flags.append(f"--requires={resources['constraint']}")
        # mem intentionally dropped: flux-core basic scheduler has no schedulable memory.
        return flags

    def submit_job(
        self,
        script_path: str | None = None,
        resources: dict | None = None,
        training_data: dict | None = None,  # unused; interface parity
        remote_dir: str | None = None,
    ) -> str:
        if not script_path:
            raise JobSubmissionError("script_path is required for Flux mode")
        local_script = Path(script_path)
        if not local_script.exists():
            raise JobSubmissionError(f"Job script not found: {local_script}")

        remote_dir = remote_dir or f"{self._remote_workdir}/{uuid.uuid4().hex[:12]}"
        self.executor.run(["mkdir", "-p", remote_dir])
        remote_script = f"{remote_dir}/run.sh"
        self.executor.put(str(local_script), remote_script)

        cmd = [
            "flux",
            "batch",
            "--output",
            f"{remote_dir}/{{{{id}}}}.out",  # flux mustache: {{id}}
            "--error",
            f"{remote_dir}/{{{{id}}}}.err",
            *self._flux_flags(resources or {}),
            remote_script,
        ]
        result = self.executor.run(cmd, timeout=_CMD_TIMEOUT)
        if result.returncode != 0:
            raise JobSubmissionError(f"flux batch failed: {result.stderr.strip()}")

        lines = result.stdout.strip().splitlines()
        if not lines:
            raise JobSubmissionError("flux batch returned no job id")
        job_id = lines[-1].strip()  # F58, e.g. "ƒAbCdEf"
        self._jobdir[job_id] = remote_dir
        return job_id

    # ── monitoring ───────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(state: str, result: str) -> str:
        state = (state or "").upper()
        if state == "INACTIVE":
            return _RESULT_MAP.get((result or "").upper(), "FAILED")
        return _ACTIVE_MAP.get(state, "UNKNOWN")

    def get_job_status(self, job_id: str) -> JobStatus:
        fmt = "{state} {result} {t_run:%FT%T} {t_cleanup:%FT%T} {returncode}"
        r = self.executor.run(
            ["flux", "jobs", "--no-header", "-o", fmt, job_id],
            timeout=_CMD_TIMEOUT,
        )
        line = r.stdout.strip()
        if not line:
            # Purged from the active journal — fall back to the persisted eventlog.
            return self._status_from_eventlog(job_id)

        parts = line.split()
        state = parts[0] if parts else "UNKNOWN"
        result = parts[1] if len(parts) > 1 else ""
        t_run = parts[2] if len(parts) > 2 else ""
        t_clean = parts[3] if len(parts) > 3 else ""
        rc_raw = parts[4] if len(parts) > 4 else ""
        exit_code = int(rc_raw) if rc_raw.lstrip("-").isdigit() else None
        return {
            "state": self._normalize(state, result),
            "exit_code": exit_code,
            "start_time": _na(t_run),
            "end_time": _na(t_clean),
        }

    def _status_from_eventlog(self, job_id: str) -> JobStatus:
        r = self.executor.run(["flux", "job", "info", job_id, "eventlog"], timeout=_CMD_TIMEOUT)
        text = r.stdout
        if not text.strip():
            raise JobNotFoundError(f"Job {job_id} not found in flux jobs or eventlog")

        # The Flux eventlog is JSONL — parse it rather than substring-matching, so a
        # standard-spaced ``"status": 0`` (valid JSON) isn't mistaken for a failure.
        saw_terminal = False
        finish_status: int | None = None
        exit_code: int | None = None
        parsed_any = False
        for raw_line in text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except ValueError:
                continue
            parsed_any = True
            name = ev.get("name")
            ctx = ev.get("context", {}) if isinstance(ev.get("context"), dict) else {}
            if name in ("finish", "clean"):
                saw_terminal = True
            if name == "finish":
                status_val = ctx.get("status")
                if isinstance(status_val, (int, float)):
                    finish_status = int(status_val)
                    # Flux encodes a wait(2)-style status: exit code is the high byte.
                    exit_code = finish_status >> 8 if finish_status >= 256 else finish_status

        if parsed_any:
            if not saw_terminal:
                state = "RUNNING"
            elif finish_status is None or finish_status == 0:
                # No terminal-failure signal → treat as completed (don't invent failures).
                state = "COMPLETED"
            else:
                state = "FAILED"
            return {"state": state, "exit_code": exit_code, "start_time": None, "end_time": None}

        # Fallback: no JSON parsed — keep the original coarse substring heuristic.
        state = "COMPLETED" if "clean" in text or "finish" in text else "RUNNING"
        if "finish" in text and '"status":0' not in text and "status=0" not in text:
            state = "FAILED"
        return {"state": state, "exit_code": None, "start_time": None, "end_time": None}

    def cancel_job(self, job_id: str) -> None:
        """Cancel a job with ``flux cancel``. Raises on a non-zero exit.

        A serving job (`exa serve llm stop`) never ends on its own, so without this its
        allocation runs until its time limit.
        """
        result = self.executor.run(["flux", "cancel", job_id], timeout=_CMD_TIMEOUT)
        if result.returncode != 0:
            raise SchedulerAdapterError(f"flux cancel {job_id} failed: {result.stderr.strip()}")

    def get_job_logs(self, job_id: str) -> str:
        remote_dir = self._jobdir.get(job_id, self._remote_workdir)
        remote_out = f"{remote_dir}/{job_id}.out"
        local = self.working_dir / f"{job_id}.out"
        try:
            self.executor.get(remote_out, str(local))
            return local.read_text()
        except (FileNotFoundError, OSError):
            r = self.executor.run(["flux", "job", "attach", "-E", job_id], timeout=_CMD_TIMEOUT)
            return r.stdout or f"[FluxAdapter] No log found for job {job_id}"

    def remote_jobdir(self, job_id: str) -> str:
        """Remote per-job directory (where the training script writes ``model.pkl``)."""
        return self._jobdir.get(job_id, self._remote_workdir)


def _na(value: str) -> str | None:
    value = (value or "").strip()
    return value if value and value != "N/A" else None
