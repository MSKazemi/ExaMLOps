from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import get_db, init_db

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    os.environ.pop("PLATFORM_DB", None)


def test_explain_history_empty():
    """explain history on a fresh DB returns no entries."""
    result = runner.invoke(app, ["serve", "explain", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "No explain history" in result.output


def test_explain_404_logs_unavailable():
    """When serving returns 404, exit 0, print warning, log 'unavailable' in DB."""
    http_err = urllib.error.HTTPError(
        url="http://localhost/explain/JPCP",
        code=404,
        msg="Not Found",
        hdrs=MagicMock(),  # type: ignore[arg-type]
        fp=None,  # type: ignore[arg-type]
    )

    with patch("urllib.request.urlopen", side_effect=http_err):
        result = runner.invoke(app, ["serve", "explain", "explain", "JPCP"])

    assert result.exit_code == 0, result.output
    assert "not available" in result.output.lower() or "unavailable" in result.output.lower()

    # DB should have a log entry with status=unavailable
    with get_db() as conn:
        rows = conn.execute("SELECT status, error FROM explain_logs WHERE model='JPCP'").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "unavailable"
    assert "404" in rows[0]["error"]


def test_explain_success_shows_table():
    """When endpoint returns features, table is printed."""
    payload = {
        "features": [
            {"name": "f0", "importance": 0.9},
            {"name": "f1", "importance": 0.3},
            {"name": "f2", "importance": -0.1},
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = runner.invoke(app, ["serve", "explain", "explain", "JPCP"])

    assert result.exit_code == 0, result.output
    assert "f0" in result.output
    assert "0.9" in result.output


def test_explain_success_top_n_respected():
    """--top-n limits the number of features shown."""
    payload = {"features": [{"name": f"feat_{i}", "importance": float(i) * 0.1} for i in range(20)]}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = runner.invoke(app, ["serve", "explain", "explain", "JPCP", "--top-n", "3"])

    assert result.exit_code == 0, result.output
    # Only 3 rows in table — count occurrences of "feat_" which appear once per row
    assert result.output.count("feat_") <= 3


def test_explain_history_after_requests():
    """History shows log entries after previous explain calls."""
    http_err = urllib.error.HTTPError(
        url="http://localhost/explain/JPCP",
        code=404,
        msg="Not Found",
        hdrs=MagicMock(),  # type: ignore[arg-type]
        fp=None,  # type: ignore[arg-type]
    )
    payload = {"features": [{"name": "emb_norm", "importance": 0.8}]}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    # First call: 404
    with patch("urllib.request.urlopen", side_effect=http_err):
        runner.invoke(app, ["serve", "explain", "explain", "JPCP"])

    # Second call: success
    with patch("urllib.request.urlopen", return_value=mock_resp):
        runner.invoke(app, ["serve", "explain", "explain", "JPCP"])

    result = runner.invoke(app, ["serve", "explain", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "unavailable" in result.output
    assert "ok" in result.output


# ── the command written the way people write it ──────────────────────────────────────────────
#
# `exa explain exa status` used to answer "Unknown command: 'exa status'" about a command that
# plainly exists, because the leading `exa` was matched as a subcommand name. The MCP tool made it
# circular: it echoed the canonical form back as `"command": "exa status"` — the very string it
# would then reject — so an agent following its own output hit an error.


@pytest.mark.parametrize(
    "written,canonical",
    [
        (["exa", "status"], "exa status"),
        (["status"], "exa status"),
        (["exa", "serve", "reload"], "exa serve reload"),
        (["exa", "--help"], "exa"),
        (["exa"], "exa"),
        ([], "exa"),
        (["serve", "reload", "--help"], "exa serve reload"),
    ],
)
def test_a_leading_exa_and_any_options_are_not_command_names(written, canonical):
    from examlops.cli.commands.explain_command import _normalize, _resolve

    assert _resolve(written) is not None, f"{written} should resolve"
    assert " ".join(["exa", *_normalize(written)]).strip() == canonical


def test_a_genuinely_unknown_command_is_still_unknown():
    """Normalising must not turn a typo into a match — `exa` is stripped, nothing else is."""
    from examlops.cli.commands.explain_command import _resolve

    assert _resolve(["exa", "nope"]) is None
    assert _resolve(["nope"]) is None


def test_the_mcp_tool_accepts_the_command_string_it_prints():
    """Round-trip: whatever `explain_command` echoes must be valid input to `explain_command`."""
    from examlops.mcp import tools

    first = tools.explain_command("serve reload")
    assert first["ok"] is True
    again = tools.explain_command(first["command"])
    assert again["ok"] is True, f"echoed {first['command']!r} was rejected: {again}"
    assert again["command"] == first["command"]


def test_the_mcp_tool_explains_a_prefixed_command():
    from examlops.mcp import tools

    out = tools.explain_command("exa status")
    assert out["ok"] is True
    assert out["command"] == "exa status"
    assert out["summary"]
