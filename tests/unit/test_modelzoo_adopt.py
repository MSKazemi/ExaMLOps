"""Guard for ``exa modelzoo adopt`` — project-per-model provisioner (M2).

Proves the provisioner composes the real project primitives, is idempotent, and that ``--dry-run``
writes nothing.
"""

from __future__ import annotations

import pytest

import examlops.modelzoo_adopt as mz
import examlops.usecase as usecase
from examlops.data.projects import (
    get_project,
    get_project_budget,
    get_project_storage,
    list_project_models,
)
from examlops.platform_db import get_db, init_db
from examlops.workbenches import get_workbench


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    models = tmp_path / "models"
    models.mkdir()
    (models / "jpcp.yaml").write_text("name: JPCP\nenabled: true\n")
    (models / "mack.yaml").write_text("name: MACK\nenabled: true\n")
    (models / "_private.yaml").write_text("name: _private\n")  # underscore = skipped
    monkeypatch.setattr(usecase, "models_dir", lambda default=None: models)
    init_db()
    yield tmp_path


def test_zoo_models_lists_pack_skipping_private():
    assert mz.zoo_models() == ["JPCP", "MACK"]


def test_project_name_is_lowercased():
    assert mz.project_name_for("JPCP") == "jpcp"


def test_adopt_model_provisions_everything():
    out = mz.adopt_model("JPCP")
    assert out["changed"] is True and out["project"] == "jpcp"

    assert get_project("jpcp") is not None
    assert get_project_storage("jpcp") is not None
    assert get_project_budget("jpcp") is not None
    assert "JPCP" in list_project_models("jpcp")
    assert get_workbench(mz.WORKBENCH_NAME, "jpcp") is not None
    # both pipeline surfaces registered
    with get_db() as conn:
        rows = conn.execute("SELECT kind FROM project_pipelines WHERE project='jpcp'").fetchall()
    assert {r["kind"] for r in rows} == {"prefect", "rayserve"}

    # audit row written
    with get_db() as conn:
        audits = conn.execute("SELECT * FROM audit_events WHERE action='modelzoo_adopt'").fetchall()
    assert len(audits) == 1 and audits[0]["target"] == "jpcp"


def test_adopt_is_idempotent():
    mz.adopt_model("JPCP")
    out2 = mz.adopt_model("JPCP")
    assert out2["changed"] is False
    assert out2["steps"]["project"] == "exists" and out2["steps"]["workbench"] == "exists"
    # no second audit row for an unchanged re-run
    with get_db() as conn:
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE action='modelzoo_adopt'"
        ).fetchone()
    assert audits["c"] == 1


def test_dry_run_writes_nothing():
    out = mz.adopt_model("JPCP", dry_run=True)
    assert out["dry_run"] is True and out["changed"] is True
    assert out["steps"]["project"] == "would-create"
    assert get_project("jpcp") is None  # nothing persisted


def test_adopt_all_covers_every_model():
    results = mz.adopt_all()
    assert {r["project"] for r in results} == {"jpcp", "mack"}
    assert all(r["changed"] for r in results)
