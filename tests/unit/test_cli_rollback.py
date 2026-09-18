from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner  # noqa: E402

from examlops.cli.commands import models as _models_cmd  # noqa: E402
from examlops.cli.commands import rollback_cmd  # noqa: E402
from examlops.cli.main import app  # noqa: E402

# Wire rollback_cmd into models app so tests work without modifying main.py.
# Guard against repeated imports in the same session.
if not any(
    getattr(t, "name", None) == "rollback"
    for t in getattr(_models_cmd.app, "registered_groups", [])
):
    _models_cmd.app.add_typer(
        rollback_cmd.app,
        name="rollback",
        help="Roll back a model alias to a previous version",
    )

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    from examlops.platform_db import init_db

    init_db()
    yield
    os.environ.pop("PLATFORM_DB", None)


# ---------------------------------------------------------------------------
# Shared mock data
# ---------------------------------------------------------------------------

_VERSIONS_DATA = {
    "model_versions": [
        {"version": "7", "current_stage": "Production", "status": "READY"},
        {"version": "6", "current_stage": "Staging", "status": "READY"},
        {"version": "5", "current_stage": "None", "status": "READY"},
    ]
}

_REGISTERED_MODEL_DATA = {
    "registered_model": {
        "name": "jpcp",
        "aliases": [{"alias": "Production", "version": "7"}],
    }
}

_ALIAS_SET_RESPONSE: dict = {}


def _make_urlopen(versions_data=None, model_data=None, alias_response=None):
    """Return a mock for urllib.request.urlopen that handles all three endpoint shapes."""
    versions_data = versions_data if versions_data is not None else _VERSIONS_DATA
    model_data = model_data if model_data is not None else _REGISTERED_MODEL_DATA
    alias_response = alias_response if alias_response is not None else _ALIAS_SET_RESPONSE

    def _urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = getattr(req, "method", "GET") or "GET"
        if "model-versions/search" in url:
            payload = json.dumps(versions_data).encode()
        elif "registered-models/alias" in url and method == "POST":
            payload = json.dumps(alias_response).encode()
        elif "registered-models/get" in url:
            payload = json.dumps(model_data).encode()
        else:
            payload = b"{}"
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: payload)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    return _urlopen


# ---------------------------------------------------------------------------
# Test 1: history is empty at start → exit 0
# ---------------------------------------------------------------------------


def test_rollback_history_empty():
    result = runner.invoke(app, ["models", "rollback", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output or "No rollback history" in result.output


# ---------------------------------------------------------------------------
# Test 2: rollback run JPCP --version 5 stores a DB record
# ---------------------------------------------------------------------------


def test_rollback_version_stores_record():
    with patch("urllib.request.urlopen", side_effect=_make_urlopen()):
        result = runner.invoke(
            app,
            ["--yes", "models", "rollback", "run", "JPCP", "--version", "5"],
        )
    assert result.exit_code == 0, result.output
    assert "rolled back" in result.output.lower() or "5" in result.output

    # Verify DB record
    from examlops import platform_db

    with platform_db.get_db() as conn:
        rows = conn.execute("SELECT * FROM model_rollbacks WHERE model='JPCP'").fetchall()
    assert len(rows) == 1
    assert rows[0]["to_version"] == 5
    assert rows[0]["from_version"] == 7
    assert rows[0]["alias"] == "Production"


# ---------------------------------------------------------------------------
# Test 3: history shows the record after rollback
# ---------------------------------------------------------------------------


def test_rollback_history_shows_record():
    with patch("urllib.request.urlopen", side_effect=_make_urlopen()):
        runner.invoke(
            app,
            ["--yes", "models", "rollback", "run", "JPCP", "--version", "5"],
        )

    result = runner.invoke(app, ["models", "rollback", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "5" in result.output
    assert "7" in result.output
    assert "Production" in result.output


# ---------------------------------------------------------------------------
# Test 4: MLflow unreachable → error message, exit code 1
# ---------------------------------------------------------------------------


def test_rollback_mlflow_unreachable():
    def _failing_urlopen(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=_failing_urlopen):
        result = runner.invoke(
            app,
            ["--yes", "models", "rollback", "run", "JPCP", "--version", "5"],
        )
    assert result.exit_code == 1, result.output
    assert "unreachable" in result.output.lower() or "mlflow" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 5: dry-run does not write to DB and prints "dry-run"
# ---------------------------------------------------------------------------


def test_rollback_dry_run_no_db_write():
    with patch("urllib.request.urlopen", side_effect=_make_urlopen()):
        result = runner.invoke(
            app,
            ["--yes", "models", "rollback", "run", "JPCP", "--version", "5", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "dry" in result.output.lower()

    from examlops import platform_db

    # The table may not exist yet — a dry run must not create it. Either way, no rows.
    with platform_db.get_db() as conn:
        try:
            rows = conn.execute("SELECT * FROM model_rollbacks").fetchall()
        except Exception:  # noqa: BLE001 — "no such table" differs per engine
            rows = []
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Test 6: rollback with --reason stores reason in DB
# ---------------------------------------------------------------------------


def test_rollback_reason_stored():
    with patch("urllib.request.urlopen", side_effect=_make_urlopen()):
        result = runner.invoke(
            app,
            [
                "--yes",
                "models",
                "rollback",
                "run",
                "JPCP",
                "--version",
                "6",
                "--reason",
                "bad metrics",
            ],
        )
    assert result.exit_code == 0, result.output

    from examlops import platform_db

    with platform_db.get_db() as conn:
        row = conn.execute("SELECT reason FROM model_rollbacks WHERE model='JPCP'").fetchone()
    assert row is not None
    assert row["reason"] == "bad metrics"


def test_fetch_versions_follows_mlflows_page_token():
    """`_fetch_versions` promises *all* versions, and rollback is what needs the old ones.

    MLflow pages `model-versions/search`. Reading one page and stopping caps how far back a
    rollback can reach — on exactly the operation whose whole purpose is to reach backwards — and
    it does it silently: the older versions are simply absent from the candidate list, so the
    command reports the target version does not exist.
    """
    pages = [
        {
            "model_versions": [{"version": "9", "status": "READY"}],
            "next_page_token": "p2",
        },
        {
            "model_versions": [{"version": "8", "status": "READY"}],
            "next_page_token": "p3",
        },
        {"model_versions": [{"version": "3", "status": "READY"}]},
    ]
    seen_tokens = []

    def _urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        token = None
        if "page_token=" in url:
            token = url.split("page_token=")[1].split("&")[0]
        seen_tokens.append(token)
        payload = json.dumps(pages[len(seen_tokens) - 1]).encode()
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: payload)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    cfg = MagicMock(mlflow_url="http://mlflow")
    with patch("urllib.request.urlopen", side_effect=_urlopen):
        versions = rollback_cmd._fetch_versions(cfg, "jpcp")

    assert [v["version"] for v in versions] == ["9", "8", "3"], versions
    assert seen_tokens == [None, "p2", "p3"], seen_tokens


def test_fetch_versions_refuses_a_registry_that_repeats_its_page_token():
    """A token that never advances must end the loop loudly, not quietly.

    Returning the pages read so far would be the original short-list bug wearing a loop, and
    looping forever would hang the command. `_output.error` exits non-zero, which is the honest
    answer: the registry is misbehaving and the rollback candidates cannot be trusted.
    """
    calls = []

    def _urlopen(req, timeout=10):
        calls.append(req.full_url)
        payload = json.dumps(
            {"model_versions": [{"version": "9", "status": "READY"}], "next_page_token": "same"}
        ).encode()
        ctx = MagicMock()
        ctx.__enter__ = lambda s: MagicMock(read=lambda: payload)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    cfg = MagicMock(mlflow_url="http://mlflow")
    import typer

    with patch("urllib.request.urlopen", side_effect=_urlopen), pytest.raises(typer.Exit):
        rollback_cmd._fetch_versions(cfg, "jpcp")
    assert len(calls) == 2, calls  # the first page, then the repeat that is caught
