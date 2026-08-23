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


# ── the SLO gate must not pass an SLO it never evaluated in silence (T87) ────────────────
#
# Setting EXAMLOPS_SLO_GATE_ENABLED and flagging an SLO `gate_promotion` reads as "promotion is
# now gated on this". With no samples recorded it is gated on nothing: the ratio the gate reads
# had no denominator. The command promotes either way — a model cannot produce SLI samples
# before it serves, and does not serve before it is promoted, so refusing would deadlock every
# first promotion — but it must say that the gate did not run.


def _gate_flagged_slo_with_no_samples() -> None:
    from examlops.data.governance import upsert_slo_spec

    upsert_slo_spec("jpcp", "latency", target=0.99, gate_promotion=True)


def test_promote_says_the_slo_gate_could_not_evaluate_a_zero_sample_slo(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SLO_GATE_ENABLED", "1")
    _gate_flagged_slo_with_no_samples()
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}),
    ):
        result = runner.invoke(app, ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"])
    assert result.exit_code == 0, result.output
    assert "could not evaluate" in result.output.lower(), result.output
    assert "latency" in result.output
    # ...and it still promotes, rather than deadlocking a model that has never served.
    assert "promoted" in result.output.lower()


def test_promote_is_quiet_about_an_slo_that_was_never_asked_to_gate(monkeypatch):
    """The control: an unmeasured SLO with `gate_promotion` unset is not a gate that failed."""
    monkeypatch.setenv("EXAMLOPS_SLO_GATE_ENABLED", "1")
    from examlops.data.governance import upsert_slo_spec

    upsert_slo_spec("jpcp", "latency", target=0.99, gate_promotion=False)
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_patched_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}),
    ):
        result = runner.invoke(app, ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"])
    assert result.exit_code == 0, result.output
    assert "could not evaluate" not in result.output.lower(), result.output
