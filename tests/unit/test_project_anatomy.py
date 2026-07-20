"""Project Anatomy — P6 storage · P7 pipelines · P8 anatomy view (ADR 0091/0092/0093).

GWT coverage: per-project storage location + quota mirror + connection bind + fail-open usage;
the two pipeline surfaces (aggregation + one-each registry + empty project); the assembled,
secret-safe, fail-open anatomy from get_project_full.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    # A Fernet key so P2 connections can store a secret via the D7 local store.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    from examlops import platform_db

    platform_db.init_db()
    yield


@pytest.fixture
def demo():
    from examlops import platform_db as db

    db.create_project("demo", storage_gb=100.0)
    db.assign_resource_to_project("demo", "model", "JPCP")
    return db


# ── P6 storage ────────────────────────────────────────────────────────────────
def test_default_storage_location(demo):
    # GWT-1: default location = shared bucket + per-project prefix, quota mirrored.
    s = demo.ensure_project_storage("demo")
    assert s["bucket"] == "examlops-projects"
    assert s["prefix"] == "demo/"
    assert s["quota_gb"] == 100.0


def test_projects_bucket_env_override(demo, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROJECTS_BUCKET", "my-bucket")
    assert demo.projects_bucket() == "my-bucket"
    assert demo.ensure_project_storage("demo")["bucket"] == "my-bucket"


def test_storage_idempotent(demo):
    # GWT-2: two ensures → one row, no error.
    demo.ensure_project_storage("demo")
    demo.ensure_project_storage("demo")
    with demo.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM project_storage WHERE project='demo'"
        ).fetchone()
    assert n["n"] == 1


def test_storage_unknown_project(demo):
    # GWT-3: unknown project → no row, returns None.
    assert demo.ensure_project_storage("ghost") is None
    assert demo.get_project_storage("ghost") is None


def test_project_experiment_name(demo):
    assert demo.project_experiment("demo") == "project/demo"


def test_bind_connection(demo, monkeypatch):
    # GWT-4: binding an s3 connection sets connection_ref + bucket from the connection; no secret stored.
    from examlops import connections

    connections.create_connection(
        "raw", kind="s3", project="demo", config={"bucket": "raw-bucket"}, secret_value="SEKRET"
    )
    assert demo.bind_project_connection("demo", "raw") is True
    rec = demo.get_project_storage("demo")
    assert rec["connection_ref"] == "raw"
    assert rec["bucket"] == "raw-bucket"
    # no secret leaked into project_storage
    assert "SEKRET" not in str(rec)


def test_bind_missing_connection(demo):
    assert demo.bind_project_connection("demo", "nope") is False


def test_refresh_usage_fail_open(demo, monkeypatch):
    # GWT-5: MinIO unreachable → returns prior used_bytes, no raise.
    demo.ensure_project_storage("demo")
    demo.set_project_usage("demo", 4242)
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://127.0.0.1:1")  # nothing listening
    assert demo.refresh_project_usage("demo") == 4242


# ── P7 pipelines ──────────────────────────────────────────────────────────────
def test_prefect_and_rayserve_surfaces(demo):
    # GWT-1/2: both surfaces derive from the project's models + traffic.
    demo.set_traffic_rules("JPCP", {"Production": 90, "Canary": 10})
    demo.upsert_project_pipeline(
        "demo", "prefect", "examlops-jpcp", schedule="0 2 * * *", status="healthy"
    )
    pipes = demo.get_project_pipelines("demo")
    assert pipes["prefect"]["deployments"] == ["examlops-jpcp"]
    assert pipes["prefect"]["schedule"] == "0 2 * * *"
    assert pipes["rayserve"]["models"] == ["JPCP"]
    assert pipes["rayserve"]["traffic"]["JPCP"] == {"Production": 90, "Canary": 10}


def test_pipeline_one_each_pk(demo):
    # GWT-3: PK (project, kind) → second upsert updates the first.
    demo.upsert_project_pipeline("demo", "prefect", "a", status="unknown")
    demo.upsert_project_pipeline("demo", "prefect", "b", status="healthy")
    with demo.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM project_pipelines WHERE project='demo' AND kind='prefect'"
        ).fetchall()
    assert len(rows) == 1 and rows[0]["ref"] == "b"


def test_pipeline_invalid_kind(demo):
    with pytest.raises(ValueError):
        demo.upsert_project_pipeline("demo", "airflow", "x")


def test_empty_project_pipelines(demo):
    # GWT-5: a project with no models/registry → both surfaces None.
    demo.create_project("empty")
    assert demo.get_project_pipelines("empty") == {"prefect": None, "rayserve": None}


# ── P8 anatomy ────────────────────────────────────────────────────────────────
def test_full_anatomy_extended(demo):
    # GWT-1: get_project_full adds storage/connections/pipelines.
    full = demo.get_project_full("demo")
    assert set(full) >= {"storage", "connections", "pipelines", "resources", "members"}
    assert full["storage"]["bucket"] == "examlops-projects"
    assert full["pipelines"]["prefect"]["deployments"] == ["examlops-jpcp"]


def test_anatomy_secret_safe(demo):
    # GWT-2: connections in the anatomy expose has_secret only, never the secret value.
    from examlops import connections

    connections.create_connection(
        "raw", kind="s3", project="demo", config={"bucket": "b"}, secret_value="TOPSECRET"
    )
    full = demo.get_project_full("demo")
    conns = full["connections"]
    assert conns and conns[0]["has_secret"] is True
    assert "TOPSECRET" not in str(conns)
    assert all("secret_ref" not in c and "config" not in c for c in conns)


def test_anatomy_unknown_project(demo):
    # GWT-4: unknown project → None.
    assert demo.get_project_full("ghost") is None


def test_anatomy_fail_open(demo, monkeypatch):
    # GWT-3: a broken source doesn't blow up the anatomy.
    import examlops.platform_db as db

    # get_project_pipelines' body now lives in examlops.data.projects, and get_project_full (also there)
    # calls it as a same-module lookup — so patch the new home (item 4.5 relocation).
    monkeypatch.setattr(
        "examlops.data.projects.get_project_pipelines",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    full = db.get_project_full("demo")
    assert full is not None
    assert full["pipelines"] == {"prefect": None, "rayserve": None}
