"""ADR 0014 decision 4 beyond ``exa project``: the model-mutating ``exa`` commands are gated.

Real code throughout: the CLI app, the real ``authz_relations`` table, the real project membership
tables, the real traffic / auto-retrain stores and the real audit log. The subject is the CLI
actor (``EXAMLOPS_ACTOR``). The control plane's routes ask the same question
(``platform/services/control_plane/tests/test_project_gate.py``).
"""

from __future__ import annotations

import inspect
import json

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in ("EXAMLOPS_MULTITENANCY", "EXAMLOPS_AUTHZ_ADMINS", "EXAMLOPS_OPENFGA_URL"):
        monkeypatch.delenv(var, raising=False)
    # Unreachable MLflow: a gated command must refuse before it would ever get there.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")
    monkeypatch.setenv(
        "RAY_SERVE_URL", "http://127.0.0.1:1"
    )  # the live traffic push is best-effort
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    from examlops.platform_db import init_db

    init_db()
    return monkeypatch


@pytest.fixture
def tenant(env):
    """Tenancy on; JPCP belongs to ``acme``; bob edits it, vic views it."""
    from examlops.authz import grant
    from examlops.data.projects import assign_model_to_project, create_project

    env.setenv("EXAMLOPS_MULTITENANCY", "1")
    create_project("acme")
    assert assign_model_to_project("acme", "JPCP")
    grant("bob", "editor", "project:acme", actor="test")
    grant("vic", "viewer", "project:acme", actor="test")
    return env


def _as(env, who: str, *args: str):
    env.setenv("EXAMLOPS_ACTOR", who)
    return runner.invoke(app, ["--yes", *args])


def _traffic():
    from examlops.data.serving import get_traffic_rules

    return get_traffic_rules("JPCP")


def _denials() -> list[dict]:
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if e["action"] == "authz_deny"]


SET_TRAFFIC = ("serve", "traffic", "JPCP", "--production", "90", "--canary", "10")


# --- the list of gated commands is true --------------------------------------------------------


def test_every_listed_command_really_calls_the_gate():
    from examlops.cli import _model_authz
    from examlops.cli.commands import (
        cards_a6_cmd,
        data_cmd,
        drift,
        pipeline,
        project_cmd,
        serve,
        synth_cmd,
    )

    functions = {
        "pipeline promote": pipeline.promote,
        "pipeline run": pipeline._run_body,
        "serve traffic": serve.traffic,
        "drift auto-retrain enable": drift.auto_retrain_enable,
        "drift auto-retrain disable": drift.auto_retrain_disable,
        "data snapshot": data_cmd.snapshot,
        "data list": data_cmd.list_revisions,
        "data diff": data_cmd.diff,
        "data checkout": data_cmd.checkout,
        "data validate": data_cmd.validate,
        "data synth generate": synth_cmd.generate,
        "cards dataset": cards_a6_cmd.dataset_card,
        "project assign": project_cmd._assign,
        "project assign-model": project_cmd._assign,
    }
    assert set(functions) == set(_model_authz.GUARDED_COMMANDS)
    for name, fn in functions.items():
        src = inspect.getsource(fn)
        relation = _model_authz.GUARDED_COMMANDS[name]
        assert (
            "guard_model(" in src
            or f'guard_dataset(dataset, "{relation}")' in src
            or f'guard_resource(kind, ref, "{relation}")' in src
        ), name


# --- flag off -----------------------------------------------------------------------------------


def test_flag_off_anyone_may_set_traffic(env):
    from examlops.data.projects import assign_model_to_project, create_project

    create_project("acme")
    assign_model_to_project("acme", "JPCP")
    assert _as(env, "mallory", *SET_TRAFFIC).exit_code == 0
    assert _traffic() == {"Production": 90, "Canary": 10}
    assert _denials() == []


# --- flag on ------------------------------------------------------------------------------------


def test_a_stranger_cannot_set_traffic_and_nothing_changes(tenant):
    res = _as(tenant, "mallory", *SET_TRAFFIC)
    assert res.exit_code == 1, res.output
    assert "Permission denied" in res.output
    assert _traffic() is None
    denial = _denials()[-1]
    assert denial["target"] == "project:acme/model:JPCP"
    assert json.loads(denial["details"]) == {"subject": "mallory", "relation": "editor"}


def test_an_editor_may_set_traffic(tenant):
    assert _as(tenant, "bob", *SET_TRAFFIC).exit_code == 0
    assert _traffic() == {"Production": 90, "Canary": 10}


def test_a_viewer_may_read_but_not_set_traffic(tenant):
    assert _as(tenant, "vic", "serve", "traffic", "JPCP").exit_code == 0
    assert _as(tenant, "vic", *SET_TRAFFIC).exit_code == 1
    assert _traffic() is None
    assert _as(tenant, "mallory", "serve", "traffic", "JPCP").exit_code == 1


def test_the_model_name_is_matched_case_insensitively(tenant):
    """Assigned as JPCP, addressed as jpcp: still acme's model, still refused."""
    assert _as(tenant, "mallory", "serve", "traffic", "jpcp", "--production", "100").exit_code == 1


def test_auto_retrain_enable_and_disable_are_gated(tenant):
    from examlops.data.drift import get_drift_auto_retrain

    enable = ("drift", "auto-retrain", "enable", "JPCP", "--dataset", "PM100Dataset")
    assert _as(tenant, "mallory", *enable).exit_code == 1
    assert get_drift_auto_retrain("JPCP") is None
    assert _as(tenant, "bob", *enable).exit_code == 0
    assert get_drift_auto_retrain("JPCP")["enabled"]
    assert _as(tenant, "mallory", "drift", "auto-retrain", "disable", "JPCP").exit_code == 1
    assert get_drift_auto_retrain("JPCP")["enabled"]
    assert _as(tenant, "bob", "drift", "auto-retrain", "disable", "JPCP").exit_code == 0
    assert not get_drift_auto_retrain("JPCP")["enabled"]


def test_promote_is_refused_before_mlflow_is_contacted(tenant):
    res = _as(tenant, "mallory", "pipeline", "promote", "JPCP", "--if-rmse-lt", "5")
    assert res.exit_code == 1
    assert "Permission denied" in res.output
    assert "MLflow" not in res.output


def test_pipeline_run_is_gated_on_the_model_and_on_the_claimed_project(tenant):
    from examlops.data.projects import create_project, get_project_for_model

    res = _as(tenant, "mallory", "pipeline", "run", "--model", "JPCP", "--dummy")
    assert res.exit_code == 1 and "Permission denied" in res.output
    # No model named: the whole registry, which only a platform admin may train at once.
    res = _as(tenant, "bob", "pipeline", "run", "--dummy")
    assert res.exit_code == 1 and "every model" in res.output
    # `--project` would attach the model to that project: a claim bob may not make on `rival`.
    create_project("rival")
    res = _as(tenant, "bob", "pipeline", "run", "--model", "JPCP", "--project", "rival", "--dummy")
    assert res.exit_code == 1 and "Permission denied" in res.output
    assert get_project_for_model("JPCP") == "acme"


def test_a_platform_admin_passes(tenant):
    tenant.setenv("EXAMLOPS_AUTHZ_ADMINS", "root-ops")
    assert _as(tenant, "root-ops", *SET_TRAFFIC).exit_code == 0


# --- the shared guard (examlops.authz.guard) ---------------------------------------------------


def test_unassigned_models_belong_to_default_where_legacy_grants_apply(tenant):
    from examlops.authz import guard

    assert guard.model_projects("UNCLAIMED") == ["default"]
    assert guard.model_allowed("legacy:operator", "editor", "UNCLAIMED")
    assert not guard.model_allowed("legacy:viewer", "editor", "UNCLAIMED")
    assert not guard.model_allowed("legacy:operator", "editor", "JPCP")  # acme's


def test_a_shared_model_needs_every_owning_project(tenant):
    from examlops.authz import guard
    from examlops.data.projects import assign_model_to_project, create_project

    create_project("beta")
    assign_model_to_project("beta", "JPCP")
    assert guard.model_projects("JPCP") == ["acme", "beta"]
    assert not guard.model_allowed("bob", "editor", "JPCP")
    from examlops.authz import grant

    grant("bob", "editor", "project:beta", actor="test")
    assert guard.model_allowed("bob", "editor", "JPCP")


def test_idp_asserted_project_roles_map_to_relations(tenant):
    from examlops.authz import guard

    assert guard.model_allowed("u1", "editor", "JPCP", asserted_projects={"acme": "operator"})
    assert not guard.model_allowed("u1", "owner", "JPCP", asserted_projects={"acme": "operator"})
    assert not guard.model_allowed("u1", "editor", "JPCP", asserted_projects={"acme": "viewer"})
    assert not guard.model_allowed("u1", "viewer", "JPCP", asserted_projects={"other": "admin"})


def test_an_unreadable_membership_store_fails_closed(tenant):
    import examlops.data as data
    from examlops.authz import guard

    def broken():
        raise RuntimeError("datastore down")

    tenant.setattr(data, "get_db", broken)
    with pytest.raises(guard.ModelScopeUnavailable):
        guard.model_allowed("bob", "editor", "JPCP")


def test_unreadable_store_is_a_cli_refusal(tenant):
    import examlops.data as data

    def broken():
        raise RuntimeError("datastore down")

    tenant.setenv("EXAMLOPS_ACTOR", "bob")
    tenant.setattr(data, "get_db", broken)
    res = runner.invoke(app, ["--yes", "drift", "auto-retrain", "disable", "JPCP"])
    assert res.exit_code == 1
    assert "refusing" in res.output


# --- datasets (ADR 0014 decision 3: dataset-keyed tables reach their project via the dataset) ----


@pytest.fixture
def datasets(tenant):
    from examlops.data.projects import assign_resource_to_project

    assert assign_resource_to_project("acme", "dataset", "FData")
    return tenant


def _revisions() -> list[dict]:
    from examlops.data.data_assets import get_dataset_revisions

    return get_dataset_revisions("FData")


def test_a_stranger_cannot_snapshot_or_read_a_projects_dataset(datasets, tmp_path):
    res = _as(datasets, "mallory", "data", "snapshot", "FData", "--backend", "minio")
    assert res.exit_code == 1 and "Permission denied" in res.output
    assert _revisions() == []
    assert _as(datasets, "mallory", "data", "list", "FData").exit_code == 1
    assert _as(datasets, "mallory", "data", "diff", "FData", "a", "b").exit_code == 1
    assert _as(datasets, "mallory", "data", "checkout", "FData", "a").exit_code == 1
    assert _denials()[-1]["target"] == "project:acme/dataset:FData"


def test_an_editor_may_snapshot_and_a_viewer_may_only_read(datasets):
    assert _as(datasets, "vic", "data", "snapshot", "FData", "--backend", "minio").exit_code == 1
    assert _revisions() == []
    res = _as(datasets, "bob", "data", "snapshot", "FData", "--backend", "minio")
    assert res.exit_code == 0, res.output
    assert len(_revisions()) == 1
    assert _as(datasets, "vic", "data", "list", "FData").exit_code == 0


def test_an_unassigned_dataset_is_the_default_projects(tenant):
    from examlops.authz import guard

    assert guard.resource_projects("dataset", "Unclaimed") == ["default"]
    assert _as(tenant, "mallory", "data", "list", "Unclaimed").exit_code == 1
    assert guard.resource_allowed("legacy:viewer", "viewer", "dataset", "Unclaimed")


def test_an_unknown_resource_kind_is_a_programming_error(tenant):
    from examlops.authz import guard

    with pytest.raises(ValueError):
        guard.resource_projects("prompt", "p")


# --- review fixes: membership is the authorization key, so every road to it is guarded ----------


def test_a_project_owner_cannot_claim_another_projects_model(tenant):
    """`exa project assign` moves who controls a model: editing the target is not enough."""
    from examlops.authz import grant, guard
    from examlops.data.projects import create_project

    create_project("mine")
    grant("mallory", "owner", "project:mine", actor="test")
    res = _as(tenant, "mallory", "project", "assign", "mine", "JPCP", "--kind", "model")
    assert res.exit_code == 1 and "Permission denied" in res.output
    assert guard.model_projects("JPCP") == ["acme"]  # nothing was attached
    res = _as(tenant, "mallory", "project", "assign-model", "mine", "JPCP")
    assert res.exit_code == 1
    assert guard.model_projects("JPCP") == ["acme"]


def test_a_project_owner_cannot_claim_an_unassigned_model_or_dataset(tenant):
    """An unassigned resource is `default`'s: claiming it takes it away from `default`'s editors."""
    from examlops.authz import grant, guard
    from examlops.data.projects import create_project

    create_project("mine")
    grant("mallory", "owner", "project:mine", actor="test")
    assert _as(tenant, "mallory", "project", "assign", "mine", "UNCLAIMED").exit_code == 1
    assert guard.model_projects("UNCLAIMED") == ["default"]
    res = _as(tenant, "mallory", "project", "assign", "mine", "FData", "--kind", "dataset")
    assert res.exit_code == 1
    assert guard.resource_projects("dataset", "FData") == ["default"]
    # An editor where the resource lives now may move it.
    grant("mallory", "editor", "project:default", actor="test")
    assert _as(tenant, "mallory", "project", "assign", "mine", "UNCLAIMED").exit_code == 0
    assert guard.model_projects("UNCLAIMED") == ["mine"]


def test_a_legacy_namespace_assignment_scopes_the_model(tenant):
    """A model grouped via `exa namespace` is billed to that project, so it is guarded as its."""
    from examlops.authz import grant, guard
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO namespace_models (model, namespace) VALUES ('NSMODEL', 'acme')")
        # The column's own default is not a second owner of a model a real project holds.
        conn.execute("INSERT INTO namespace_models (model, namespace) VALUES ('JPCP', 'default')")
        conn.commit()
    assert guard.model_projects("NSMODEL") == ["acme"]
    assert guard.model_projects("JPCP") == ["acme"]
    grant("dora", "editor", "project:default", actor="test")
    res = _as(tenant, "dora", "serve", "traffic", "NSMODEL", "--production", "100")
    assert res.exit_code == 1 and "Permission denied" in res.output
    assert _as(tenant, "bob", "serve", "traffic", "NSMODEL", "--production", "100").exit_code == 0
    assert _as(tenant, "bob", *SET_TRAFFIC).exit_code == 0


def test_pipeline_run_needs_viewer_on_the_dataset_it_trains_on(datasets):
    """Training reads the dataset: a model editor may not train on another project's data."""
    from examlops.authz import grant

    grant("dora", "editor", "project:default", actor="test")
    res = _as(datasets, "dora", "pipeline", "run", "--model", "UNCLAIMED", "--dataset", "FData")
    assert res.exit_code == 1, res.output
    assert "Permission denied" in res.output and "dataset 'FData'" in res.output
    assert _denials()[-1]["target"] == "project:acme/dataset:FData"


def test_every_dataset_writer_is_refused_for_a_stranger(datasets, tmp_path):
    """Each command that records a row under a dataset refuses before any side effect."""
    from examlops.data.data_assets import get_data_quality_checks, list_synthetic_datasets
    from examlops.data.registry import get_dataset_card

    for argv in (
        ("data", "validate", "FData", "--path", str(tmp_path)),
        ("data", "synth", "generate", "FData", "--path", str(tmp_path), "--rows", "5"),
        ("cards", "dataset", "FData"),
    ):
        res = _as(datasets, "mallory", *argv)
        assert res.exit_code == 1, (argv, res.output)
        assert "Permission denied" in res.output, (argv, res.output)
        assert _denials()[-1]["target"] == "project:acme/dataset:FData"
        # a viewer is not enough to write either
        res = _as(datasets, "vic", *argv)
        assert res.exit_code == 1 and "Permission denied" in res.output, (argv, res.output)
    assert get_data_quality_checks("FData") == []
    assert list_synthetic_datasets("FData") == []
    assert get_dataset_card("FData") is None
    assert _revisions() == []
