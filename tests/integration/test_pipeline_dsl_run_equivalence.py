"""ADR 0080 — the IR run trains through the *real* generator and matches the equivalent YAML run.

Opt-in (``EXAMLOPS_LIVE_PIPELINE_EQUIV=1``): each side starts Prefect's temporary server and trains
JPCP on the dummy dataset, ~25 s apiece — too slow for the unit suite, needs no external service
(MLflow is a throw-away SQLite store). ``tests/unit/test_pipeline_dsl.py`` holds the always-on
structural proof (the lowered YAML loads to the identical ``ModelYAMLConfig``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "pipeline-as-code" / "jpcp_flow.py"

if os.environ.get("EXAMLOPS_LIVE_PIPELINE_EQUIV") != "1":
    pytest.skip("set EXAMLOPS_LIVE_PIPELINE_EQUIV=1 to run", allow_module_level=True)
if not (_ROOT / "modelzoo" / "seanergys_modelzoo").is_dir():
    pytest.skip("seanergys_modelzoo not present", allow_module_level=True)


def _env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EXAMLOPS_HPC", "PREFECT_"))}
    env.update(
        PLATFORM_DB=str(tmp / "platform.db"),
        MLFLOW_TRACKING_URI=f"sqlite:///{tmp / 'mlflow.db'}",
        MLFLOW_ARTIFACT_ROOT=str(tmp / "art"),
        PREFECT_API_URL="",
        EXAMLOPS_SLURM_MODE="mock",
        EXAMLOPS_SEED="7",  # dummy data is random; a seed makes the two runs comparable
        PYTHONPATH=str(_ROOT / "platform" / "cli" / "src"),
    )
    return env


def _run(cmd: list[str], tmp: Path) -> str:
    proc = subprocess.run(
        cmd, cwd=_ROOT, env=_env(tmp), capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def _facts(output: str) -> dict[str, str]:
    return {
        "metrics": re.search(r"\[evaluate\] (RMSE:.*)", output).group(1).strip(),
        "registered": re.search(r"Registered (\w+ v\d+) \(framework=(\w+)\)", output).group(0),
        "staging": re.search(r"Set @Staging on (\w+ v\d+)", output).group(1),
    }


def test_ir_run_matches_the_yaml_run_on_the_dummy_dataset(tmp_path):
    ir = tmp_path / "jpcp.ir.json"
    _run(
        [
            sys.executable,
            "-m",
            "examlops.cli",
            "pipeline",
            "compile",
            str(_EXAMPLE),
            "--out",
            str(ir),
        ],
        tmp_path,
    )

    yaml_dir, ir_dir = tmp_path / "yaml", tmp_path / "ir"
    yaml_dir.mkdir()
    ir_dir.mkdir()
    yaml_out = _run(
        [
            sys.executable,
            "pipelines/pipeline_generator.py",
            "--model",
            "JPCP",
            "--dataset",
            "PM100Dataset",
            "--dummy",
        ],
        yaml_dir,
    )
    ir_out = _run(
        [
            sys.executable,
            "-m",
            "examlops.cli",
            "pipeline",
            "run",
            "--ir",
            str(ir),
            "--dataset",
            "PM100Dataset",
            "--dummy",
        ],
        ir_dir,
    )
    assert _facts(ir_out) == _facts(yaml_out)
