from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import init_db

runner = CliRunner()

# ---------------------------------------------------------------------------
# Sample API response payloads
# ---------------------------------------------------------------------------

_META_RESPONSE = {
    "task_type": "anomaly_detection",
    "framework": "pytorch",
    "enabled": True,
    "datasets": ["PM100Dataset", "PM200Dataset"],
    "lifecycle_gates": [
        {"stage": "Staging", "metric": "rmse", "threshold": 5.0},
        {"stage": "Production", "metric": "rmse", "threshold": 4.0},
    ],
}

_MLFLOW_RM_RESPONSE = {
    "registered_model": {
        "name": "jpcp",
        "latest_versions": [
            {"version": "17", "current_stage": "Production", "run_id": "abcdef1234567890"},
            {"version": "18", "current_stage": "Staging", "run_id": "fedcba9876543210"},
        ],
        "aliases": [{"alias": "Production", "version": "17"}],
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    # ensure cards table exists too
    from examlops.cli.commands.cards_cmd import _ensure_model_cards_table
    _ensure_model_cards_table()
    yield
    os.environ.pop("PLATFORM_DB", None)


# ---------------------------------------------------------------------------
# Test 1: card history JPCP -> empty
# ---------------------------------------------------------------------------

def test_card_history_empty():
    result = runner.invoke(app, ["models", "card", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    # Should report no history without crashing
    assert "No model card history" in result.output or "JPCP" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 2: card JPCP with mocked control plane + MLflow -> prints markdown
# ---------------------------------------------------------------------------

def test_card_generates_markdown_stdout():
    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = _side_effect_get

        result = runner.invoke(app, ["models", "card", "generate", "JPCP"])

    assert result.exit_code == 0, result.output
    assert "# Model Card: JPCP" in result.output
    assert "anomaly_detection" in result.output
    assert "pytorch" in result.output
    assert "PM100Dataset" in result.output
    assert "Model card generated" in result.output


def test_card_db_record_created():
    """card command must create a DB row even when writing to stdout."""
    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = _side_effect_get
        runner.invoke(app, ["models", "card", "generate", "JPCP"])

    from examlops.platform_db import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM model_cards WHERE model=?", ("JPCP",)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "JPCP"
    assert rows[0]["output_path"] is None


# ---------------------------------------------------------------------------
# Test 3: card JPCP --output /tmp/testcard.md -> file written
# ---------------------------------------------------------------------------

def test_card_writes_file(tmp_path):
    out_file = tmp_path / "jpcp-card.md"

    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = _side_effect_get

        result = runner.invoke(app, ["models", "card", "generate", "JPCP", "--output", str(out_file)])

    assert result.exit_code == 0, result.output
    assert out_file.exists(), "Output file was not created"

    content = out_file.read_text()
    assert "# Model Card: JPCP" in content
    assert "anomaly_detection" in content
    assert "pytorch" in content
    assert "written to" in result.output or "written" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 4: card history shows entry after card generation
# ---------------------------------------------------------------------------

def test_card_history_shows_entry(tmp_path):
    out_file = tmp_path / "jpcp-card.md"

    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = _side_effect_get
        runner.invoke(app, ["models", "card", "generate", "JPCP", "--output", str(out_file)])

    result = runner.invoke(app, ["models", "card", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    # The output path is stored in the DB; Rich may truncate the cell in terminal output.
    # Verify the DB record directly instead of relying on Rich table rendering.
    from examlops.platform_db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT output_path FROM model_cards WHERE model=? LIMIT 1", ("JPCP",)
        ).fetchone()
    assert row is not None
    assert row["output_path"] == str(out_file)


# ---------------------------------------------------------------------------
# Test 5: card JPCP when control plane unreachable -> graceful error, exit 1
# ---------------------------------------------------------------------------

def test_card_control_plane_unreachable():
    from examlops.cli._client import ClientError

    with patch("examlops.cli._client.get", side_effect=ClientError("Service unreachable at http://localhost:18002")):
        result = runner.invoke(app, ["models", "card", "generate", "JPCP"])

    assert result.exit_code == 1, result.output
    # Should surface an error message, not a traceback
    assert "unreachable" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 6: card history (all models, no filter)
# ---------------------------------------------------------------------------

def test_card_history_all_models():
    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = _side_effect_get
        runner.invoke(app, ["models", "card", "generate", "JPCP"])

    with patch("examlops.cli._client.get") as mock_get:
        mock_get.side_effect = lambda url: (
            {"task_type": "classification", "framework": "sklearn",
             "enabled": True, "datasets": ["IrisDataset"], "lifecycle_gates": []}
            if "/models/" in url and "/meta" in url
            else _MLFLOW_RM_RESPONSE
        )
        runner.invoke(app, ["models", "card", "generate", "OtherModel"])

    result = runner.invoke(app, ["models", "card", "history"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output


# ---------------------------------------------------------------------------
# Test 7: MLflow unreachable does NOT abort card generation
# ---------------------------------------------------------------------------

def test_card_mlflow_unreachable_still_generates():
    from examlops.cli._client import ClientError

    def _side_effect(url: str):
        if "/meta" in url:
            return _META_RESPONSE
        raise ClientError("MLflow unreachable")

    with patch("examlops.cli._client.get", side_effect=_side_effect):
        result = runner.invoke(app, ["models", "card", "generate", "JPCP"])

    assert result.exit_code == 0, result.output
    assert "# Model Card: JPCP" in result.output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _side_effect_get(url: str):
    """Route mock GET calls based on URL pattern."""
    if "/meta" in url:
        return _META_RESPONSE
    if "registered-models/get" in url:
        return _MLFLOW_RM_RESPONSE
    return {}
