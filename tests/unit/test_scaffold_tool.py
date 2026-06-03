from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "scaffold_model.py"


def _run(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--name", "TestAD", "--task", "anomaly_detection",
         "--task-type", "classification", *extra_args],
        capture_output=True, text=True,
    )


def test_stdout_json_returns_five_files():
    r = _run("--stdout-json")
    assert r.returncode == 0, r.stderr
    files: dict = json.loads(r.stdout)
    keys = list(files.keys())
    assert any("testad_model.py" in k for k in keys)
    assert any("__init__.py" in k for k in keys)
    assert any("testad_config.py" in k for k in keys)
    assert any("test_testad.py" in k for k in keys)
    assert any("testad.yaml" in k for k in keys)


def test_stdout_json_no_files_written(tmp_path):
    r = _run("--stdout-json", "--repo-root", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not any(tmp_path.rglob("*.py"))


def test_repo_root_writes_to_given_dir(tmp_path):
    r = _run("--repo-root", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "pipelines" / "models" / "testad.yaml").exists()
    assert (tmp_path / "pipelines" / "model_configs" / "testad_config.py").exists()
    assert (tmp_path / "tests" / "unit" / "test_testad.py").exists()
