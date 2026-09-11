"""ADR 0130 — exa dataplane."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.main import app  # noqa: E402
from examlops.data import init_db  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402
from examlops.dataplane.types import Probe, TableBatch  # noqa: E402

runner = CliRunner()


class _Two(BaseConnector):
    kind = "two"
    connection_required = False

    def probe(self, conn, spec=None):
        return Probe(True, "fine")

    def read(self, conn, spec, since, limits):
        yield TableBatch("rows", pa.RecordBatch.from_pylist([{"i": 1}, {"i": 2}]))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    init_db()
    registry.reset()
    registry.register(_Two())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    yield
    registry.reset()


def _json(args):
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_connectors_lists_availability():
    kinds = {c["kind"]: c for c in _json(["dataplane", "connectors"])}
    assert kinds["two"]["available"] is True and "sql" in kinds


def test_create_pull_and_inspect():
    _json(["dataplane", "sources", "create", "s", "--connector", "two", "--spec-json", "{}"])
    pull = _json(["dataplane", "pull", "s"])
    assert pull["status"] == "succeeded" and pull["row_count"] == 2
    assert _json(["dataplane", "snapshots", "s"])[0]["revision"] == pull["revision"]
    assert _json(["dataplane", "manifest", "s"])["row_count"] == 2
    assert _json(["dataplane", "preview", "s", "--limit", "1"]) == [{"i": 1}]


def test_dry_run_creates_nothing():
    out = _json(["dataplane", "sources", "create", "s", "--connector", "two", "--dry-run"])
    assert out["dry_run"] is True
    assert _json(["dataplane", "sources", "list"]) == []


def test_unknown_connector_is_a_clean_error():
    result = runner.invoke(app, ["dataplane", "sources", "create", "s", "--connector", "nope"])
    assert result.exit_code != 0 and "unknown connector" in result.output


def test_apply_rejects_credentials_in_files(tmp_path):
    f = tmp_path / "sources.yaml"
    f.write_text("sources:\n  - name: s\n    connector: two\n    spec: {password: x}\n")
    result = runner.invoke(app, ["dataplane", "sources", "apply", "--file", str(f)])
    assert result.exit_code != 0 and "credential" in result.output


def test_apply_dry_run_also_rejects_credentials(tmp_path):
    f = tmp_path / "sources.yaml"
    f.write_text("sources:\n  - name: s\n    connector: two\n    spec: {password: x}\n")
    result = runner.invoke(app, ["dataplane", "sources", "apply", "--file", str(f), "--dry-run"])
    assert result.exit_code != 0 and "credential" in result.output
    assert _json(["dataplane", "sources", "list"]) == []


def test_pull_remote_posts_to_the_dataplane_service(tmp_path, monkeypatch):
    """``--remote`` asks the dataplane service (config: dataplane_url/dataplane_token)."""
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_TOKEN", "tok-1")
    _json(["dataplane", "sources", "create", "s", "--connector", "two", "--spec-json", "{}"])
    calls: dict = {}

    def _fake_post(url, body, token="", **kwargs):
        calls["url"], calls["body"], calls["token"] = url, body, token
        return {"pull_id": "abc123"}

    monkeypatch.setattr("examlops.cli._client.post", _fake_post)
    out = _json(["dataplane", "pull", "s", "--remote"])
    assert calls["url"] == "http://localhost:18010/sources/s/pull"
    assert calls["token"] == "tok-1"
    assert out == {"pull_id": "abc123"}
