"""Scheduler fakes shared by the tests that drive the real phase-23 adapters.

`ScriptRunningExecutor` is a cluster in a box: handed to the real `RealSlurmAdapter` or
`FluxAdapter`, its `sbatch` / `flux batch` run the submitted script with bash, now, and the status
commands report how it exited — so a test exercises the adapters' own code and the generated
script is executed, not just inspected. (A stand-in *adapter* that accepts anything is how two
scheduler paths that could not run on any real scheduler once passed their tests.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from executor import CompletedCommand  # noqa: E402


class ScriptRunningExecutor:
    """A cluster in a box: `sbatch` / `flux batch` run the submitted script with bash, now, and
    the status commands report how it exited. Everything else is a no-op that succeeds."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.calls: list[list[str]] = []
        self.returncode: int | None = None

    def run(self, cmd, *, timeout=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[0] == "mkdir":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[0] == "sbatch" or cmd[:2] == ["flux", "batch"]:
            self.returncode = subprocess.run(["bash", cmd[-1]], capture_output=True).returncode
            out = f"Submitted batch job {self.job_id}" if cmd[0] == "sbatch" else self.job_id
            return CompletedCommand(0, out, "")
        elif cmd[0] == "sacct" and "--format=State,ExitCode,Start,End" in cmd:
            state = "COMPLETED" if self.returncode == 0 else "FAILED"
            return CompletedCommand(0, f"{state}|{self.returncode}:0|t0|t1", "")
        elif cmd[:2] == ["flux", "jobs"]:
            result = "COMPLETED" if self.returncode == 0 else "FAILED"
            return CompletedCommand(0, f"INACTIVE {result} t0 t1 {self.returncode}", "")
        return CompletedCommand(0, "", "")

    def put(self, local, remote):
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local, remote)

    def get(self, remote, local):
        raise FileNotFoundError(remote)

    def close(self):
        pass
