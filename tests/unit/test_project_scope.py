"""P3 — Project scoping for serving & pipelines (ADR 0088, spec P3).

GWT-1/2 project resolution + Prefect tags · precedence (explicit → env → membership) ·
unscoped model degrades cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import project_scope as ps  # noqa: E402
from examlops.platform_db import assign_resource_to_project, create_project, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_PROJECT", raising=False)
    init_db()
    create_project("research")


def test_resolve_from_membership():
    assign_resource_to_project("research", "model", "JPCP")
    assert ps.resolve_project("JPCP") == "research"


def test_unscoped_model_is_none():
    assert ps.resolve_project("NOPE") is None


def test_explicit_wins():
    assign_resource_to_project("research", "model", "JPCP")
    assert ps.resolve_project("JPCP", explicit="override") == "override"


def test_env_precedence(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROJECT", "envproj")
    assert ps.resolve_project("JPCP") == "envproj"
    # explicit still beats env
    assert ps.resolve_project("JPCP", explicit="x") == "x"


def test_metric_label_defaults_to_none_string():
    assert ps.metric_label("NOPE") == "none"


def test_prefect_tags_appends_project():
    assign_resource_to_project("research", "model", "JPCP")
    tags = ps.prefect_tags("JPCP")
    assert "examlops" in tags and "training" in tags
    assert "project:research" in tags


def test_prefect_tags_unscoped_unchanged():
    assert ps.prefect_tags("NOPE") == ["examlops", "training"]


def test_prefect_tags_does_not_mutate_base():
    base = ["a"]
    ps.prefect_tags("NOPE", base=base)
    assert base == ["a"]


def test_model_yaml_project_field():
    from pipelines.model_loader import ModelYAMLConfig

    cfg = ModelYAMLConfig(name="X", config_class="C", task_type="t", project="research")
    assert cfg.project == "research"
    assert ModelYAMLConfig(name="Y", config_class="C", task_type="t").project is None
