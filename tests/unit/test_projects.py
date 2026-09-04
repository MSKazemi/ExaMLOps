"""Unit tests for ExaMLOps Projects (RHOAI-style Docker resource envelopes).

Tests cover:
- platform_db project CRUD helpers
- CLI commands (create / list / show / set-quota / assign-model / compose / archive / delete)
- Docker Compose fragment generation
- resource quota enforcement checks
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import (
    add_project_member,
    archive_project,
    assign_model_to_project,
    assign_resource_to_project,
    create_project,
    delete_project,
    get_db,
    get_project,
    get_project_consumption,
    get_project_full,
    init_db,
    list_project_members,
    list_project_models,
    list_projects,
    list_relations,
    record_model_cost,
    remove_project_member,
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
        result = runner.invoke(
            app,
            [
                "project",
                "create",
                "fullproj",
                "--cpu-limit",
                "8",
                "--memory-gb",
                "32",
                "--storage-gb",
                "200",
                "--gpu-limit",
                "2",
                "--description",
                "Full project",
            ],
        )
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
        runner.invoke(
            app, ["project", "create", "model-proj", "--cpu-limit", "4", "--memory-gb", "8"]
        )
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
        runner.invoke(
            app, ["project", "create", "split-proj", "--cpu-limit", "4", "--memory-gb", "8"]
        )
        runner.invoke(app, ["project", "assign-model", "split-proj", "A"])
        runner.invoke(app, ["project", "assign-model", "split-proj", "B"])
        out_file = str(tmp_path / "compose.yml")
        runner.invoke(app, ["project", "compose", "split-proj", "--out", out_file])
        import yaml as _yaml

        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        # 4 / 2 = 2.0 CPUs per service
        assert float(
            doc["services"]["a"]["deploy"]["resources"]["limits"]["cpus"]
        ) == pytest.approx(2.0)

    def test_compose_gpu_adds_device_reservation(self, tmp_path):
        runner.invoke(
            app, ["project", "create", "gpu-proj", "--cpu-limit", "4", "--gpu-limit", "1"]
        )
        runner.invoke(app, ["project", "assign-model", "gpu-proj", "JPCP"])
        out_file = str(tmp_path / "compose.yml")
        runner.invoke(app, ["project", "compose", "gpu-proj", "--out", out_file])
        import yaml as _yaml

        with open(out_file) as f:
            doc = _yaml.safe_load(f)
        devices = doc["services"]["jpcp"]["deploy"]["resources"]["reservations"]["devices"]
        assert any(d["driver"] == "nvidia" for d in devices)

    def test_compose_includes_x_metadata(self, tmp_path):
        runner.invoke(
            app, ["project", "create", "meta-proj", "--cpu-limit", "2", "--memory-gb", "4"]
        )
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


# ── Unified Project Workspace (ADR 0086, spec P1) ─────────────────────────────


class TestUnifiedProjectWorkspace:
    def test_gwt1_generic_membership_dual_writes_model(self):
        create_project("demo")
        assert assign_resource_to_project("demo", "model", "JPCP")
        with get_db() as c:
            assert c.execute(
                "SELECT 1 FROM project_models WHERE project='demo' AND model='JPCP'"
            ).fetchone()
            assert c.execute(
                "SELECT 1 FROM project_resources "
                "WHERE project='demo' AND kind='model' AND ref='JPCP'"
            ).fetchone()

    def test_generic_membership_non_model_kind(self):
        create_project("demo")
        assert assign_resource_to_project("demo", "pipeline", "jpcp-train")
        from examlops.platform_db import list_project_resources

        assert list_project_resources("demo")["pipeline"] == ["jpcp-train"]

    def test_unknown_kind_rejected(self):
        create_project("demo")
        with pytest.raises(ValueError):
            assign_resource_to_project("demo", "bogus", "x")

    def test_assign_to_missing_project_returns_false(self):
        assert assign_resource_to_project("nope", "model", "X") is False

    def test_gwt2_members_via_authz_and_audit(self):
        create_project("demo")
        add_project_member("demo", "alice", "editor", actor="admin")
        members = list_project_members("demo")
        assert [(m["subject"], m["role"]) for m in members] == [("alice", "editor")]
        assert list_relations(obj="project:demo")
        with get_db() as c:
            assert c.execute(
                "SELECT 1 FROM audit_events WHERE action='authz_grant' AND target='project:demo'"
            ).fetchone()

    def test_member_role_validation(self):
        create_project("demo")
        with pytest.raises(ValueError):
            add_project_member("demo", "alice", "superuser")

    def test_remove_member(self):
        create_project("demo")
        add_project_member("demo", "bob", "viewer", actor="admin")
        assert remove_project_member("demo", "bob") >= 1
        assert list_project_members("demo") == []

    def test_gwt3_anatomy(self):
        create_project("demo")
        assign_resource_to_project("demo", "model", "JPCP")
        add_project_member("demo", "alice", "owner", actor="admin")
        full = get_project_full("demo")
        assert full["resources"]["model"] == ["JPCP"]
        assert full["members"][0]["subject"] == "alice"
        assert full["consumption"] == {"gpu_hours": 0.0, "cost_usd": 0.0}

    def test_anatomy_unknown_project_is_none(self):
        assert get_project_full("ghost") is None

    def test_gwt4_model_costs_project_column_idempotent(self):
        init_db()
        with get_db() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(model_costs)").fetchall()}
        assert "project" in cols
        init_db()  # re-run migration must not raise
        init_db()

    def test_cost_auto_attributed_to_project(self):
        create_project("demo")
        assign_resource_to_project("demo", "model", "JPCP")
        record_model_cost("JPCP", 1, None, None, 2.5, 10.0)
        with get_db() as c:
            row = c.execute("SELECT project FROM model_costs WHERE model_name='JPCP'").fetchone()
        assert row["project"] == "demo"

    def test_gwt5_consumption_union_counts_namespace_only_model(self):
        create_project("demo")
        with get_db() as c:
            c.execute("INSERT INTO namespace_models (namespace, model) VALUES ('demo', 'LEGACY')")
        record_model_cost("LEGACY", 1, None, None, 3.0, 12.0, project=None)
        consumption = get_project_consumption("demo")
        assert consumption["gpu_hours"] == pytest.approx(3.0)
        assert consumption["cost_usd"] == pytest.approx(12.0)

    def test_gwt6_active_project_env_overrides(self, tmp_path, monkeypatch):
        from examlops.cli import _config

        monkeypatch.setattr(_config, "CONFIG_PATH", tmp_path / "config.toml")
        _config.set_active_project("demo")
        assert _config.active_project() == "demo"
        monkeypatch.setenv("EXAMLOPS_PROJECT", "other")
        assert _config.active_project() == "other"

    def test_cli_assign_and_members_flow(self):
        runner.invoke(app, ["project", "create", "flow"])
        assert (
            runner.invoke(app, ["project", "assign", "flow", "JPCP", "--kind", "model"]).exit_code
            == 0
        )
        assert (
            runner.invoke(
                app, ["project", "add-member", "flow", "alice", "--role", "editor"]
            ).exit_code
            == 0
        )
        res = runner.invoke(app, ["--json", "project", "show", "flow"])
        assert res.exit_code == 0
        import json

        data = json.loads(res.output)
        assert data["resources"]["model"] == ["JPCP"]
        assert data["members"][0]["subject"] == "alice"


class TestDeleteCascade:
    """C3 regression guard: delete must remove the FULL cascade, especially authz grants —
    otherwise re-creating a same-named project resurrects every previous member's role."""

    def test_delete_removes_authz_grants(self):
        create_project("cascade-proj")
        add_project_member("cascade-proj", "alice", role="owner")
        assert any(m["subject"] == "alice" for m in list_project_members("cascade-proj"))

        assert delete_project("cascade-proj") is True
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM authz_relations WHERE object=?", ("project:cascade-proj",)
            ).fetchall()
        assert rows == [], "authz grants must not survive project deletion"

        # The resurrection scenario itself: a re-created project has NO members.
        create_project("cascade-proj")
        assert list_project_members("cascade-proj") == []

    def test_delete_removes_budget_storage_pipeline_rows(self):
        from examlops.platform_db import (
            ensure_project_storage,
            set_project_budget,
            upsert_project_pipeline,
        )

        create_project("cascade-proj2")
        set_project_budget("cascade-proj2", gpu_hours_budget=10.0, cost_budget=100.0)
        ensure_project_storage("cascade-proj2")
        upsert_project_pipeline("cascade-proj2", "prefect", "t", status="ok")

        assert delete_project("cascade-proj2") is True
        with get_db() as conn:
            for tbl in ("project_budgets", "project_storage", "project_pipelines"):
                rows = conn.execute(
                    f"SELECT * FROM {tbl} WHERE project=?", ("cascade-proj2",)
                ).fetchall()
                assert rows == [], f"{tbl} rows must not survive project deletion"
