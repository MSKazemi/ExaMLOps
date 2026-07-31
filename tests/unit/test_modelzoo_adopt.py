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


# ── per-project MinIO/S3 connection step (the "connect minio" default) ────────────────────────────
from cryptography.fernet import Fernet  # noqa: E402

from examlops.connections import get_connection  # noqa: E402


@pytest.fixture
def _s3_env(monkeypatch):
    """Platform S3 env + a secrets KEK so the connection step can store its secret (as in prod)."""
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())


def test_adopt_provisions_and_binds_minio_connection(_s3_env):
    out = mz.adopt_model("JPCP")
    assert out["steps"]["connection"] == "created"
    conn = get_connection("minio", project="jpcp")
    assert conn is not None and conn["kind"] == "s3" and conn["has_secret"] is True
    assert conn["config"]["endpoint"] == "http://minio:9000"
    assert conn["config"]["bucket"]  # projects bucket resolved
    # storage is bound to the connection
    assert get_project_storage("jpcp")["connection_ref"] == "minio"


def test_connection_step_idempotent(_s3_env):
    mz.adopt_model("JPCP")
    out2 = mz.adopt_model("JPCP")
    assert out2["steps"]["connection"] == "exists"
    assert out2["changed"] is False


def test_connection_skipped_without_s3_env():
    # no MLFLOW_S3_ENDPOINT_URL set → connection provisioning is a no-op, rest still provisioned
    out = mz.adopt_model("JPCP")
    assert out["steps"]["connection"] == "skipped"
    assert out["steps"]["project"] == "created"
    assert get_connection("minio", project="jpcp") is None
    assert get_project_storage("jpcp")["connection_ref"] is None


def test_connection_degrades_without_secret_store(monkeypatch):
    # S3 endpoint present but NO secrets KEK → connection is still registered (config-only, no secret)
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    monkeypatch.delenv("EXAMLOPS_SECRETS_KEY", raising=False)
    monkeypatch.delenv("EXAMLOPS_SECRETS_KEYS", raising=False)
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    out = mz.adopt_model("JPCP")
    assert out["steps"]["connection"] == "created"
    conn = get_connection("minio", project="jpcp")
    assert conn is not None and conn["has_secret"] is False  # degraded: no stored secret
    assert get_project_storage("jpcp")["connection_ref"] == "minio"


def test_no_connection_flag_skips(_s3_env):
    out = mz.adopt_model("JPCP", provision_connection=False)
    assert out["steps"]["connection"] == "skipped"
    assert get_connection("minio", project="jpcp") is None


def test_dry_run_connection_would_skip_without_env():
    out = mz.adopt_model("JPCP", dry_run=True)
    assert out["steps"]["connection"] == "would-skip"  # no S3 env → would be skipped


def test_dry_run_connection_would_create_with_env(_s3_env):
    out = mz.adopt_model("JPCP", dry_run=True)
    assert out["steps"]["connection"] == "would-create"
    assert get_connection("minio", project="jpcp") is None  # nothing persisted
