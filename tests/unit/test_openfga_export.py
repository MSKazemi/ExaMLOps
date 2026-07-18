"""OpenFGA authorization-model export (enterprise-readiness Phase 2, item 2.5).

Proves the exported OpenFGA model mirrors the D6 relation hierarchy — owner⊇editor⊇viewer, project
parents granting on children — and that live D6 relations export as OpenFGA tuples, so RBAC can move
to a dedicated OpenFGA service with an equivalent (not drifting) model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.authz.openfga import authorization_model, relation_tuples, to_dsl  # noqa: E402


def _type(model, name):
    return next(t for t in model["type_definitions"] if t["type"] == name)


def test_model_has_core_types():
    model = authorization_model()
    names = {t["type"] for t in model["type_definitions"]}
    assert {"user", "project", "model"} <= names
    assert model["schema_version"] == "1.1"


def test_owner_implies_editor_implies_viewer():
    project = _type(authorization_model(), "project")
    # viewer's union includes a computed userset off 'editor'; editor off 'owner'.
    viewer_children = project["relations"]["viewer"]["union"]["child"]
    assert {"computedUserset": {"relation": "editor"}} in viewer_children
    editor_children = project["relations"]["editor"]["union"]["child"]
    assert {"computedUserset": {"relation": "owner"}} in editor_children


def test_child_types_inherit_from_parent():
    model_type = _type(authorization_model(), "model")
    assert "parent" in model_type["relations"]
    viewer_children = model_type["relations"]["viewer"]["union"]["child"]
    assert any(
        "tupleToUserset" in c for c in viewer_children
    )  # inherits viewer from parent project


def test_dsl_is_coherent():
    dsl = to_dsl()
    assert "schema 1.1" in dsl
    assert "define viewer: [user] or editor" in dsl
    assert "define owner: [user] or owner from parent" in dsl  # child inheritance


def test_relation_tuples_export(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb
    from examlops import authz

    pdb.init_db()
    authz.grant("alice", "owner", "project:acme", actor="admin")
    tuples = relation_tuples()
    assert {"user": "user:alice", "relation": "owner", "object": "project:acme"} in tuples


@pytest.mark.parametrize("child_type", ["model", "pipeline", "dataset", "storage"])
def test_all_child_types_present(child_type):
    names = {t["type"] for t in authorization_model()["type_definitions"]}
    assert child_type in names
