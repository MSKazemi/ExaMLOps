from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

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
    yield
    os.environ.pop("PLATFORM_DB", None)


_ALIAS_DATA = {"registered_model": {"aliases": [{"alias": "Staging", "version": "19"}]}}
_VER_DATA = {"model_version": {"run_id": "run-abc", "version": "19"}}
_RUN_DATA = {"run": {"data": {"metrics": {"rmse": 4.5}, "params": {}, "tags": []}}}


def _patched_get(url, **kwargs):
    if "registered-models/get" in url:
        return _ALIAS_DATA
    if "model-versions/get" in url:
        return _VER_DATA
    if "runs/get" in url:
        return _RUN_DATA
    return {}


def test_promote_passes_threshold():
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}),
    ):
        result = runner.invoke(
            app,
            [
                "--yes",  # auto-confirm the promotion prompt
                "pipeline",
                "promote",
                "jpcp",
                "--if-rmse-lt",
                "5.0",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "promoted" in result.output.lower()


def test_promote_fails_threshold():
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}),
    ):
        result = runner.invoke(
            app,
            [
                "pipeline",
                "promote",
                "jpcp",
                "--if-rmse-lt",
                "4.0",  # 4.5 is NOT < 4.0
            ],
        )
    assert result.exit_code == 0, result.output
    assert "not promoted" in result.output.lower()


def test_promote_dry_run_does_not_call_post():
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post") as mock_post,
    ):
        result = runner.invoke(
            app,
            [
                "pipeline",
                "promote",
                "jpcp",
                "--if-rmse-lt",
                "5.0",
                "--dry-run",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "dry" in result.output.lower()
    mock_post.assert_not_called()


def test_promote_confirm_declined_does_not_call_post():
    # Without --yes and a "n" answer, the promotion must be cancelled (no alias write).
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post") as mock_post,
    ):
        result = runner.invoke(
            app,
            ["pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"],
            input="n\n",
        )
    assert result.exit_code == 0, result.output
    assert "cancel" in result.output.lower()
    mock_post.assert_not_called()


def test_promote_non_numeric_metric_errors_cleanly():
    # A NaN/Infinity metric value must produce a clean error, not a formatting crash.
    nan_run = {"run": {"data": {"metrics": {"rmse": "NaN"}, "params": {}, "tags": []}}}

    def _get(url, **kwargs):
        if "runs/get" in url:
            return nan_run
        return _patched_get(url, **kwargs)

    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post") as mock_post,
    ):
        result = runner.invoke(app, ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"])
    assert result.exit_code != 0 or "nan" in result.output.lower()
    mock_post.assert_not_called()


def test_promote_list_shows_saved_rules():
    from examlops.platform_db import set_promotion_rule

    set_promotion_rule("JPCP", "rmse", "lt", 5.0)
    result = runner.invoke(app, ["pipeline", "promote", "--list"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "rmse" in result.output
