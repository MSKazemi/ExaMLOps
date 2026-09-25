"""ADR 0092 decision 1 — `exa project pipelines|show` hydrate from the sources the operator named.

Pins the CLI's source-resolution rule (configured URL ⇒ live; built-in default ⇒ only with
``--live``; ``--no-live`` ⇒ nothing) and that the live surface is what gets rendered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import project_pipelines as pp  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()

LIVE_PREFECT = {
    "source": "live",
    "ref": "project:climate",
    "deployments": ["examlops-jpcp-nightly"],
    "deployment_details": [],
    "schedule": "0 2 * * *",
    "work_pool": "hpc",
    "last_run_at": "2026-09-24T02:00:00Z",
    "last_run_state": "FAILED",
    "last_run_deployment": "examlops-jpcp-nightly",
    "status": "degraded",
    "truncated": False,
}
LIVE_SERVE = {
    "source": "live",
    "ref": pp.SERVE_APP,
    "served": ["JPCP"],
    "unserved": [],
    "aliases": {"JPCP": ["Canary", "Production"]},
    "health": {"JPCP": "ok"},
    "status": "healthy",
}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    for var in ("PREFECT_API_URL", "RAY_SERVE_URL", "EXAMLOPS_PROJECT_PIPELINES_LIVE"):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    platform_db.create_project("climate")
    platform_db.assign_resource_to_project("climate", "model", "JPCP")
    platform_db.ensure_project_storage("climate")  # the artifact destination the surface names
    yield


@pytest.fixture
def captured(monkeypatch):
    """Replace the network fetchers with faithful fakes and record the sources they were given."""
    seen: dict = {}

    def fake_prefect(project, src, *, client=None):
        seen["prefect"] = src.prefect_api_url
        return dict(LIVE_PREFECT)

    def fake_serve(project, models, src, *, client=None):
        seen["serve"] = src.serve_url
        return dict(LIVE_SERVE)

    monkeypatch.setattr(pp, "fetch_prefect_surface", fake_prefect)
    monkeypatch.setattr(pp, "fetch_rayserve_surface", fake_serve)
    return seen


def test_default_contacts_nothing_when_nothing_is_configured(captured):
    res = runner.invoke(app, ["--json", "project", "pipelines", "climate"])
    assert res.exit_code == 0, res.output
    assert captured == {}
    body = json.loads(res.output)
    assert body["prefect"]["source"] == "registry"


def test_configured_urls_are_hydrated_and_rendered(captured, monkeypatch):
    monkeypatch.setenv("PREFECT_API_URL", "http://orchestrator:4200/api")
    monkeypatch.setenv("RAY_SERVE_URL", "http://ray-serving:8001")
    res = runner.invoke(app, ["project", "pipelines", "climate"])
    assert res.exit_code == 0, res.output
    assert captured == {
        "prefect": "http://orchestrator:4200/api",
        "serve": "http://ray-serving:8001",
    }
    assert "examlops-jpcp-nightly" in res.output
    assert "FAILED" in res.output and "hpc" in res.output
    assert "Canary, Production" in res.output
    assert "s3://examlops-projects/climate/" in res.output


def test_live_flag_uses_builtin_defaults(captured):
    res = runner.invoke(app, ["project", "pipelines", "climate", "--live"])
    assert res.exit_code == 0, res.output
    assert captured["prefect"] == "http://localhost:14200/api"
    assert captured["serve"] == "http://localhost:18001"


def test_no_live_flag_wins_over_configuration(captured, monkeypatch):
    monkeypatch.setenv("PREFECT_API_URL", "http://orchestrator:4200/api")
    res = runner.invoke(app, ["--json", "project", "show", "climate", "--no-live"])
    assert res.exit_code == 0, res.output
    assert captured == {}
    assert json.loads(res.output)["pipelines"]["prefect"]["source"] == "registry"


def test_show_renders_live_source_column(captured, monkeypatch):
    monkeypatch.setenv("PREFECT_API_URL", "http://orchestrator:4200/api")
    res = runner.invoke(app, ["project", "show", "climate"])
    assert res.exit_code == 0, res.output
    assert "live" in res.output and "degraded" in res.output


def test_unreachable_source_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("PREFECT_API_URL", "http://orchestrator:4200/api")

    def down(project, src, *, client=None):
        raise pp.LiveSourceError("Prefect answered HTTP 503")

    monkeypatch.setattr(pp, "fetch_prefect_surface", down)
    res = runner.invoke(app, ["project", "pipelines", "climate"])
    assert res.exit_code == 0, res.output
    assert "registry" in res.output and "HTTP 503" in res.output
