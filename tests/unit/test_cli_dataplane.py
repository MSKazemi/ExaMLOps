from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()


def test_dataplane_status_fetches_health_and_stats():
    def fake_get(url: str):
        if url.endswith("/health"):
            return {"status": "ok", "mode": "both"}
        if url.endswith("/stats"):
            return {"inferences_total": 3, "errors_total": 0}
        raise AssertionError(f"unexpected URL: {url}")

    with patch("examlops.cli.commands.dataplane_cmd._client.get", side_effect=fake_get) as mock_get:
        result = runner.invoke(app, ["dataplane", "status"])

    assert result.exit_code == 0
    assert mock_get.call_count == 2
    assert "ok" in result.output
    assert "inferences_total" in result.output
