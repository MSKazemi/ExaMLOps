from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db = str(tmp_path / "test.db")
    os.environ["PLATFORM_DB"] = db
    from examlops.platform_db import init_db

    init_db()
    # Also ensure the batch_jobs table exists
    from examlops.cli.commands.batch_cmd import _ensure_table

    _ensure_table()
    yield db
    os.environ.pop("PLATFORM_DB", None)


# ---------------------------------------------------------------------------
# Test 1: batch list → empty
# ---------------------------------------------------------------------------


def test_batch_list_empty():
    result = runner.invoke(app, ["serve", "batch", "list"])
    assert result.exit_code == 0, result.output
    assert "no batch jobs" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 2: batch submit → job recorded, exit 0
# ---------------------------------------------------------------------------


def _fake_urlopen(req, timeout=10):
    """Return a fake HTTP response with {"prediction": 42}."""
    resp = MagicMock()
    resp.read.return_value = json.dumps({"prediction": 42}).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_batch_submit_records_job(tmp_path):
    input_file = tmp_path / "inputs.json"
    input_file.write_text(json.dumps([{"x": 1}, {"x": 2}, {"x": 3}]))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = runner.invoke(
            app,
            ["serve", "batch", "submit", "JPCP", str(input_file)],
        )

    assert result.exit_code == 0, result.output
    assert "3/3" in result.output or "3" in result.output
    assert "succeeded" in result.output.lower() or "complete" in result.output.lower()

    # Verify DB record
    from examlops.platform_db import get_db

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM batch_jobs WHERE model='JPCP'").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["n_inputs"] == 3
    assert row["n_success"] == 3
    assert row["n_errors"] == 0
    assert row["alias"] == "Production"


# ---------------------------------------------------------------------------
# Test 3: batch list after submit → shows job
# ---------------------------------------------------------------------------


def test_batch_list_shows_job_after_submit(tmp_path):
    input_file = tmp_path / "inputs.json"
    input_file.write_text(json.dumps([{"x": 10}]))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        runner.invoke(app, ["serve", "batch", "submit", "JPCP", str(input_file)])

    result = runner.invoke(app, ["serve", "batch", "list"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output


# ---------------------------------------------------------------------------
# Test 4: batch submit with nonexistent file → exit 1 + error message
# ---------------------------------------------------------------------------


def test_batch_submit_missing_file():
    result = runner.invoke(
        app,
        ["serve", "batch", "submit", "JPCP", "/tmp/does_not_exist_xyz.json"],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 5: JSONL input format
# ---------------------------------------------------------------------------


def test_batch_submit_jsonl_format(tmp_path):
    input_file = tmp_path / "inputs.jsonl"
    lines = [json.dumps({"x": i}) for i in range(5)]
    input_file.write_text("\n".join(lines))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = runner.invoke(
            app,
            ["serve", "batch", "submit", "JPCP", str(input_file)],
        )

    assert result.exit_code == 0, result.output
    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT n_inputs FROM batch_jobs WHERE model='JPCP'").fetchone()
    assert row is not None
    assert row["n_inputs"] == 5


# ---------------------------------------------------------------------------
# Test 6: output file is written
# ---------------------------------------------------------------------------


def test_batch_submit_writes_output_file(tmp_path):
    input_file = tmp_path / "inputs.json"
    input_file.write_text(json.dumps([{"x": 1}]))
    output_file = tmp_path / "predictions.json"

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = runner.invoke(
            app,
            [
                "serve",
                "batch",
                "submit",
                "JPCP",
                str(input_file),
                "--output",
                str(output_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert output_file.exists()
    data = json.loads(output_file.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["prediction"] == 42
    assert data[0]["error"] is None


# ---------------------------------------------------------------------------
# Test 7: batch list --model filter
# ---------------------------------------------------------------------------


def test_batch_list_model_filter(tmp_path):
    input_file = tmp_path / "inputs.json"
    input_file.write_text(json.dumps([{"x": 1}]))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        runner.invoke(app, ["serve", "batch", "submit", "JPCP", str(input_file)])
        runner.invoke(app, ["serve", "batch", "submit", "OTHER", str(input_file)])

    result = runner.invoke(app, ["serve", "batch", "list", "--model", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    # Should not show the OTHER model
    assert "OTHER" not in result.output
