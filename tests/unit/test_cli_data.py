# tests/unit/test_cli_data.py
"""A1 — `exa data` CLI (ADR 0003, spec A1). Covers GWT-2 (pinning) and GWT-5 (diff)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app  # noqa: E402
from examlops.platform_db import get_dataset_revisions, init_db  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_LAKEFS_ENDPOINT", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATASET_REVISION", raising=False)
    init_db()


def _parquet(path: Path, rows: int) -> Path:
    pq.write_table(pa.table({"x": list(range(rows))}), path)
    return path


def test_snapshot_records_and_prints(tmp_path):
    p = _parquet(tmp_path / "d.parquet", 5)
    result = runner.invoke(app, ["data", "snapshot", "FData", "--backend", "minio", "--path", str(p)])
    assert result.exit_code == 0, result.output
    rows = get_dataset_revisions("FData")
    assert len(rows) == 1
    assert rows[0]["revision_id"] in result.output or "Snapshot recorded" in result.output
    assert rows[0]["row_count"] == 5


def test_snapshot_json_output(tmp_path):
    p = _parquet(tmp_path / "d.parquet", 3)
    result = runner.invoke(
        app, ["--json", "data", "snapshot", "FData", "-b", "minio", "-p", str(p)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dataset"] == "FData"
    assert payload["kind"] == "content"
    assert payload["row_count"] == 3


def test_snapshot_without_path_records_unknown():
    result = runner.invoke(app, ["data", "snapshot", "FData", "--backend", "minio"])
    assert result.exit_code == 0
    rows = get_dataset_revisions("FData")
    assert rows[0]["revision_id"] == "unknown"


def test_list_empty(tmp_path):
    result = runner.invoke(app, ["data", "list", "FData"])
    assert result.exit_code == 0
    assert "No recorded revisions" in result.output


def test_list_after_snapshot(tmp_path):
    p = _parquet(tmp_path / "d.parquet", 2)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(p)])
    result = runner.invoke(app, ["data", "list", "FData"])
    assert result.exit_code == 0
    assert "content" in result.output


def test_gwt5_diff_row_delta(tmp_path):
    """GWT-5: diff reports a row-count delta equal to the real difference."""
    a = _parquet(tmp_path / "a.parquet", 10)
    b = _parquet(tmp_path / "b.parquet", 13)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(a)])
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(b)])
    revs = get_dataset_revisions("FData")
    rev_a = next(r["revision_id"] for r in revs if r["row_count"] == 10)
    rev_b = next(r["revision_id"] for r in revs if r["row_count"] == 13)
    result = runner.invoke(app, ["--json", "data", "diff", "FData", rev_a, rev_b])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_count_delta"] == 3


def test_diff_missing_revision_exits_nonzero(tmp_path):
    a = _parquet(tmp_path / "a.parquet", 4)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(a)])
    revs = get_dataset_revisions("FData")
    result = runner.invoke(app, ["data", "diff", "FData", revs[0]["revision_id"], "nope"])
    assert result.exit_code != 0


def test_gwt2_checkout_verifies_matching_data(tmp_path):
    """GWT-2/R11: checkout verifies content and succeeds only on a matching hash."""
    p = _parquet(tmp_path / "d.parquet", 6)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(p)])
    rev = get_dataset_revisions("FData")[0]["revision_id"]
    ok = runner.invoke(app, ["data", "checkout", "FData", rev, "--path", str(p)])
    assert ok.exit_code == 0, ok.output
    assert "Verified" in ok.output


def test_checkout_mismatch_exits_nonzero(tmp_path):
    p = _parquet(tmp_path / "d.parquet", 6)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(p)])
    rev = get_dataset_revisions("FData")[0]["revision_id"]
    # Mutate the file so its hash no longer matches the pinned revision.
    _parquet(tmp_path / "d.parquet", 99)
    bad = runner.invoke(app, ["data", "checkout", "FData", rev, "--path", str(tmp_path / "d.parquet")])
    assert bad.exit_code != 0


def test_checkout_requires_path_for_content(tmp_path):
    p = _parquet(tmp_path / "d.parquet", 6)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(p)])
    rev = get_dataset_revisions("FData")[0]["revision_id"]
    result = runner.invoke(app, ["data", "checkout", "FData", rev])
    assert result.exit_code != 0


# --- exa pipeline run --dataset-revision (spec R12) --------------------------


def test_pipeline_run_pin_unknown_revision_exits_nonzero(monkeypatch):
    """R12: pinning an unrecorded revision must fail before launching training."""
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        result = runner.invoke(
            app,
            ["pipeline", "run", "--dataset", "FData", "--dataset-revision", "ghost", "--dummy"],
        )
    assert result.exit_code != 0
    gen.assert_not_called()


def test_pipeline_run_pin_known_revision_launches(tmp_path, monkeypatch):
    """R12: a recorded revision is accepted, exported to the env, and launches."""
    p = _parquet(tmp_path / "d.parquet", 7)
    runner.invoke(app, ["data", "snapshot", "FData", "-b", "minio", "-p", str(p)])
    rev = get_dataset_revisions("FData")[0]["revision_id"]
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        result = runner.invoke(
            app,
            ["pipeline", "run", "--dataset", "FData", "--dataset-revision", rev, "--dummy"],
        )
    assert result.exit_code == 0, result.output
    gen.assert_called_once()
    import os

    assert os.environ.get("EXAMLOPS_DATASET_REVISION") == rev


def test_pipeline_run_pin_requires_dataset(monkeypatch):
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        result = runner.invoke(app, ["pipeline", "run", "--dataset-revision", "x", "--dummy"])
    assert result.exit_code != 0
    gen.assert_not_called()
