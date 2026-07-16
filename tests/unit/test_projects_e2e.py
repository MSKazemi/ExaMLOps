"""End-to-end integration test for the Projects & Workspaces initiative (ADR 0086–0090).

Exercises the full workspace lifecycle across every increment as one flow, through the public
module APIs, against a throwaway ``PLATFORM_DB``:

  P1  create a project, assign resources, add members (authz-backed)
  P2  attach a Named Connection (secret goes to the secrets client, only a ref in the DB)
  P5  define a workbench and confirm it injects the project's connection env
  P4  record a model cost and confirm it is attributed to the project (FinOps + budget)
  P3  confirm the project resolves as the model's owning project (project_scope)

This is the regression guard that the five increments stay wired together.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    # Secrets at rest need a Fernet key; provide a deterministic one for the test.
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", "TVk4sP_ws6A6sRz38Kw1jJZX0d3Jcq3V0z0b6n6kE-c=")
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_full_project_workspace_lifecycle(db, monkeypatch):
    import examlops.connections as connections
    import examlops.platform_db as pdb
    import examlops.project_finops as finops
    import examlops.project_scope as scope
    import examlops.workbenches as workbenches

    # ── P1: create + resources + members ─────────────────────────────────────
    pdb.create_project("research", description="R&D", created_by="alice", cpu_limit=8, gpu_limit=1)
    assert pdb.assign_resource_to_project("research", "model", "JPCP", added_by="alice")
    assert pdb.assign_resource_to_project("research", "pipeline", "jpcp-train", added_by="alice")
    pdb.add_project_member("research", "bob", "editor", actor="alice")

    full = pdb.get_project_full("research")
    assert full is not None
    assert "JPCP" in full["resources"]["model"]
    assert "jpcp-train" in full["resources"]["pipeline"]
    assert any(m["subject"] == "bob" and m["role"] == "editor" for m in full["members"])

    # ── P2: named connection — secret never lands in platform.db ─────────────
    connections.create_connection(
        "minio",
        "s3",
        project="research",
        config={"endpoint": "http://localhost:19000", "bucket": "data", "access_key": "minioadmin"},
        secret_value="s3cr3t-key",
        created_by="alice",
    )
    conn_row = connections.get_connection("minio", project="research")
    assert conn_row is not None
    assert conn_row["secret_ref"]  # a pointer is stored…
    # …but the raw secret value is NOWHERE in the connections table.
    with pdb.get_db() as raw:
        blob = str(raw.execute("SELECT * FROM connections").fetchall())
    assert "s3cr3t-key" not in blob
    # The connection is also registered as a project resource (ADR 0086 wiring).
    assert "minio" in pdb.get_project_full("research")["resources"].get("connection", [])

    # ── P5: workbench injects the project's connection env ───────────────────
    workbenches.create_workbench("nb", "research", created_by="alice")
    spec = workbenches.start_workbench("nb", "research", actor="alice")
    assert spec["status"] == "RUNNING"
    # connection_env exposes the connection's non-secret keys as EXA_CONN_* vars.
    env_keys = list(spec["env"].keys())
    assert any(k.startswith("EXA_CONN_MINIO") for k in env_keys), env_keys

    # ── P4: cost attribution + budget ────────────────────────────────────────
    # A model that belongs to research; cost auto-attributes to its project.
    pdb.record_model_cost("JPCP", 1, None, None, 10.0, 40.0)
    summary = finops.cost_summary("research")
    assert summary["gpu_hours"] == 10.0
    assert summary["cost_usd"] == 40.0
    assert summary["records"] == 1

    # Set a tight budget and confirm breach detection + audit.
    pdb.set_project_budget("research", 5.0, 100.0)  # 5 GPU-h budget vs 10 used → breach
    status = finops.budget_status("research", audit=True)
    assert status["over_budget"] is True
    assert any("GPU-hours" in b for b in status["breaches"])

    # ── P3: the model resolves to its owning project ─────────────────────────
    assert scope.resolve_project("JPCP") == "research"
    assert pdb.get_project_for_model("JPCP") == "research"

    # The whole flow is auditable: membership grant, secret write, and the budget breach all
    # land in the shared audit log (project_created itself is audited at the CLI layer).
    with pdb.get_db() as raw:
        actions = {r["action"] for r in raw.execute("SELECT action FROM audit_events").fetchall()}
    assert "authz_grant" in actions
    assert "project_budget_breach" in actions
