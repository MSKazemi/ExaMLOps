from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()

FAKE_MODELS_RESP = {
    "registered_models": [
        {"name": "jpcp", "aliases": [{"alias": "Production", "version": "3"}],
         "latest_versions": [{"version": "3", "current_stage": "None"}]},
    ]
}

def test_models_list():
    with patch("examlops.cli.commands.models._client.get", return_value=FAKE_MODELS_RESP):
        result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0
    assert "jpcp" in result.output

def test_models_list_json():
    with patch("examlops.cli.commands.models._client.get", return_value=FAKE_MODELS_RESP):
        result = runner.invoke(app, ["--json", "models", "list"])
    assert "jpcp" in result.output

def test_models_info():
    fake_info = {"registered_model": {"name": "jpcp", "aliases": [], "latest_versions": []}}
    with patch("examlops.cli.commands.models._client.get", return_value=fake_info):
        result = runner.invoke(app, ["models", "info", "jpcp"])
    assert result.exit_code == 0
