from __future__ import annotations

import subprocess
import sys

import typer

from examlops.cli import _output
from examlops.cli._enums import TaskType, TrainType

_SCRIPT = "tools/scaffold_model.py"

_EXAMPLES = (
    "Examples:\n\n"
    "  exa scaffold DemoAD\n\n"
    "  exa scaffold DemoAD --task anomaly_detection --type classification\n\n"
    "  exa scaffold DemoAD --force"
)


def scaffold(
    name: str = typer.Argument(..., help="PascalCase model name, e.g. DemoAD"),
    task: TaskType = typer.Option(
        TaskType.performance_prediction,
        "--task", "-t",
        help="Task type",
    ),
    task_type: TrainType = typer.Option(
        TrainType.regression,
        "--type", "-T",
        help="ML task type",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
):
    """Scaffold a new model: model class, config, unit test, and YAML."""
    cmd = [sys.executable, _SCRIPT, "--name", name, "--task", task, "--task-type", task_type]
    if force:
        cmd.append("--force")
    try:
        subprocess.run(cmd, check=True)  # noqa: S603
    except FileNotFoundError:
        _output.error("tools/scaffold_model.py not found — run exa scaffold from the repo root")
    except subprocess.CalledProcessError as e:
        _output.error(f"scaffold exited with {e.returncode}")
