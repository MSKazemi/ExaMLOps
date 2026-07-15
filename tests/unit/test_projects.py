"""Unit tests for ExaMLOps Projects (RHOAI-style Docker resource envelopes).

Tests cover:
- platform_db project CRUD helpers
- CLI commands (create / list / show / set-quota / assign-model / compose / archive / delete)
- Docker Compose fragment generation
- resource quota enforcement checks
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import (
    archive_project,
    assign_model_to_project,
    create_project,
    delete_project,
    get_project,
    list_project_models,
    list_projects,
    update_project_quota,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))


# ── platform_db helpers ───────────────────────────────────────────────────────

class TestProjectDB:
    def test_create_and_get(self):
        create_project("research", cpu_limit=4.0, memory_limit_gb=8.0, storage_gb=100.0)
        p = get_project("research")
        assert p is not None
        assert p["name"] == "research"
        assert p["cpu_limit"] == pytest.approx(4.0)
        assert p["memory_limit_gb"] == pytest.approx(8.0)
        assert p["storage_gb"] == pytest.approx(100.0)
        assert p["gpu_limit"] == 0
        assert p["status"] == "ACTIVE"
        assert p["network_name"] == "examlops-research"

    def test_get_missing_returns_none(self):
        assert get_project("does-not-exist") is None

    def test_list_projects(self):
        create_project("alpha", cpu_limit=2.0)
        create_project("beta", cpu_limit=4.0)
        rows = list_projects()
        names = [r["name"] for r in rows]
        assert "alpha" in names
        assert "beta" in names

    def test_list_projects_filter_by_status(self):
        create_project("active-proj")
        create_project("to-archive")
        archive_project("to-archive")
        active = [r["name"] for r in list_projects(status="ACTIVE")]
        archived = [r["name"] for r in list_projects(status="ARCHIVED")]
        assert "active-proj" in active
        assert "to-archive" in archived
        assert "to-archive" not in active

    def test_update_quota(self):
        create_project("myproj", cpu_limit=2.0, memory_limit_gb=4.0)
        update_project_quota("myproj", cpu_limit=8.0, memory_limit_gb=16.0)
        p = get_project("myproj")
        assert p["cpu_limit"] == pytest.approx(8.0)
        assert p["memory_limit_gb"] == pytest.approx(16.0)

    def test_update_quota_missing_returns_false(self):
        assert update_project_quota("ghost", cpu_limit=2.0) is False

    def test_archive(self):
        create_project("archived-proj")
        assert archive_project("archived-proj") is True
        p = get_project("archived-proj")
        assert p["status"] == "ARCHIVED"

    def test_archive_missing_returns_false(self):
        assert archive_project("no-such-project") is False

    def test_delete(self):
        create_project("to-delete")
        assign_model_to_project("to-delete", "JPCP")
        assert delete_project("to-delete") is True
        assert get_project("to-delete") is None
        # Model assignment also removed
        assert list_project_models("to-delete") == []

    def test_delete_missing_returns_false(self):
        assert delete_project("ghost") is False

    def test_assign_model(self):
        create_project("proj-with-models")
        assign_model_to_project("proj-with-models", "JPCP")
        assign_model_to_project("proj-with-models", "MACK")
        models = list_project_models("proj-with-models")
        assert "JPCP" in models
        assert "MACK" in models

    def test_assign_model_to_missing_project(self):
        assert assign_model_to_project("ghost", "JPCP") is False

    def test_assign_model_idempotent(self):
        create_project("idem-proj")
        assign_model_to_project("idem-proj", "JPCP")
        assign_model_to_project("idem-proj", "JPCP")  # again — should not raise
        assert list_project_models("idem-proj") == ["JPCP"]

    def test_gpu_limit_stored(self):
        create_project("gpu-proj", gpu_limit=2)
        p = get_project("gpu-proj")
        assert p["gpu_limit"] == 2

    def test_description_stored(self):
        create_project("doc-proj", description="A well-described project")
        p = get_project("doc-proj")
        assert p["description"] == "A well-described project"


# ── CLI: create ───────────────────────────────────────────────────────────────

class TestProjectCLI:
    def test_create_basic(self):
        result = runner.invoke(app, ["project", "create", "myproj", "--cpu-limit", "4"])
        assert result.exit_code == 0
        assert "myproj" in result.output
        assert get_project("myproj") is not None

    def test_create_with_all_options(self):
        result = runner.invoke(app, [
            "project", "create", "fullproj",
            "--cpu-limit", "8",
            "--memory-gb", "32",
            "--storage-gb", "200",
            "--gpu-limit", "2",
            "--description", "Full project",
        ])
        assert result.exit_code == 0
        p = get_project("fullproj")
        assert p["cpu_limit"] == pytest.approx(8.0)
        assert p["memory_limit_gb"] == pytest.approx(32.0)
        assert p["storage_gb"] == pytest.approx(200.0)
        assert p["gpu_limit"] == 2

    def test_create_duplicate_fails(self):
        runner.invoke(app, ["project", "create", "dup"])
        result = runner.invoke(app, ["project", "create", "dup"])
        assert result.exit_code != 0

    def test_list(self):
        runner.invoke(app, ["project", "create", "p1"])
        runner.invoke(app, ["project", "create", "p2"])
        result = runner.invoke(app, ["project", "list"])
        assert result.exit_code == 0
        assert "p1" in result.output
        assert "p2" in result.output

    def test_list_json(self):
        runner.invoke(app, ["project", "create", "jsonproj"])
        result = runner.invoke(app, ["--json", "project", "list"])
        assert result.exit_code == 0
        import json
        rows = json.loads(result.output)
        assert any(r["name"] == "jsonproj" for r in rows)

    def test_show(self):
        runner.invoke(app, ["project", "create", "showme", "--description", "test desc"])
        result = runner.invoke(app, ["project", "show", "showme"])
        assert result.exit_code == 0
        assert "showme" in result.output
        assert "test desc" in result.output

    def test_show_missing_fails(self):
        result = runner.invoke(app, ["project", "show", "ghost"])
        assert result.exit_code != 0

    def test_set_quota(self):
        runner.invoke(app, ["project", "create", "quotaproj", "--cpu-limit", "2"])
        result = runner.invoke(app, ["project", "set-quota", "quotaproj", "--cpu-limit", "8"])
        assert result.exit_code == 0
        p = get_project("quotaproj")
        assert p["cpu_limit"] == pytest.approx(8.0)

    def test_assign_model(self):
        runner.invoke(app, ["project", "create", "modelproj"])
        result = runner.invoke(app, ["project", "assign-model", "modelproj", "JPCP"])
        assert result.exit_code == 0
        assert "JPCP" in list_project_models("modelproj")

    def test_archive(self):
        runner.invoke(app, ["project", "create", "arch-proj"])
        result = runner.invoke(app, ["project", "archive", "arch-proj", "--yes"])
        assert result.exit_code == 0
        assert get_project("arch-proj")["status"] == "ARCHIVED"

    def test_delete(self):
        runner.invoke(app, ["project", "create", "del-proj"])
        result = runner.invoke(app, ["project", "delete", "del-proj", "--yes"])
        assert result.exit_code == 0
        assert get_project("del-proj") is None


# ── CLI: compose ─────────────────────────────────────────────────────────────

class TestProjectCompose:
    def test_compose_no_models(self, tmp_path):
        runner.invoke(app, ["project", "create", "empty-proj", "--cpu-limit", "4"])
        out_file = str(tmp_path / "compose.yml")
        result = runner.invoke(app, ["project", "compose", "empty-proj", "--out", out_file])
        assert result.exit_code == 0
        import yaml as _yaml
        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        assert doc["name"] == "examlops-empty-proj"
        assert "networks" in doc
        assert "examlops-empty-proj" in doc["networks"]

    def test_compose_with_models(self, tmp_path):
        runner.invoke(app, ["project", "create", "model-proj", "--cpu-limit", "4", "--memory-gb", "8"])
        runner.invoke(app, ["project", "assign-model", "model-proj", "JPCP"])
        runner.invoke(app, ["project", "assign-model", "model-proj", "MACK"])
        out_file = str(tmp_path / "compose.yml")
        result = runner.invoke(app, ["project", "compose", "model-proj", "--out", out_file])
        assert result.exit_code == 0
        import yaml as _yaml
        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        services = doc["services"]
        assert "jpcp" in services
        assert "mack" in services
        # Each service has resource limits
        limits = services["jpcp"]["deploy"]["resources"]["limits"]
        assert "cpus" in limits
        assert "memory" in limits

    def test_compose_resource_limits_split_across_models(self, tmp_path):
        """With 2 models and 4 CPU limit, each model gets 2 CPUs."""
        runner.invoke(app, ["project", "create", "split-proj", "--cpu-limit", "4", "--memory-gb", "8"])
        runner.invoke(app, ["project", "assign-model", "split-proj", "A"])
        runner.invoke(app, ["project", "assign-model", "split-proj", "B"])
        out_file = str(tmp_path / "compose.yml")
        runner.invoke(app, ["project", "compose", "split-proj", "--out", out_file])
        import yaml as _yaml
        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        # 4 / 2 = 2.0 CPUs per service
        assert float(doc["services"]["a"]["deploy"]["resources"]["limits"]["cpus"]) == pytest.approx(2.0)

    def test_compose_gpu_adds_device_reservation(self, tmp_path):
        runner.invoke(app, ["project", "create", "gpu-proj", "--cpu-limit", "4", "--gpu-limit", "1"])
        runner.invoke(app, ["project", "assign-model", "gpu-proj", "JPCP"])
        out_file = str(tmp_path / "compose.yml")
        runner.invoke(app, ["project", "compose", "gpu-proj", "--out", out_file])
        import yaml as _yaml
        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        devices = doc["services"]["jpcp"]["deploy"]["resources"]["reservations"]["devices"]
        assert any(d["driver"] == "nvidia" for d in devices)

    def test_compose_includes_x_metadata(self, tmp_path):
        runner.invoke(app, ["project", "create", "meta-proj", "--cpu-limit", "2", "--memory-gb", "4"])
        out_file = str(tmp_path / "compose.yml")
        runner.invoke(app, ["project", "compose", "meta-proj", "--out", out_file])
        import yaml as _yaml
        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        meta = doc["x-project-metadata"]
        assert meta["project"] == "meta-proj"
        assert meta["cpu_limit_cores"] == pytest.approx(2.0)
        assert meta["memory_limit_gb"] == pytest.approx(4.0)

    def test_compose_missing_project_fails(self):
        result = runner.invoke(app, ["project", "compose", "ghost"])
        assert result.exit_code != 0
