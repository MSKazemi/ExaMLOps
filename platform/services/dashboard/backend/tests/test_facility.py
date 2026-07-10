"""Facility console aggregators + /api/v1/facility endpoints (F6 / ADR 0059)."""

import sqlite3

import facility
import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """A seeded platform.db with hpc_jobs across two clusters."""
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE hpc_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, scheduler TEXT,
            flow_run_id TEXT, model TEXT, dataset TEXT, state TEXT,
            submit_time TEXT, start_time TEXT, end_time TEXT,
            queue_seconds REAL, run_seconds REAL, nodes INTEGER, gpus INTEGER,
            cpus INTEGER, exit_code INTEGER, mlflow_run_id TEXT,
            created_at TEXT, updated_at TEXT
        );
        """
    )
    rows = [
        # (job_id, scheduler, model, dataset, state, submit, queue_s, nodes, gpus, cpus, mlflow)
        ("j1", "slurm", "JPCP", "PM100", "RUNNING", "2026-07-01T10:00", 30.0, 2, 4, 16, "run-a"),
        ("j2", "slurm", "JPCP", "PM100", "SUBMITTED", "2026-07-01T10:05", 120.0, 1, 2, 8, None),
        ("j3", "slurm", "DEMO", "PM100", "SUBMITTED", "2026-07-01T10:06", 45.0, 1, 1, 4, None),
        ("j4", "flux", "JPCP", "PM100", "RUNNING", "2026-07-01T10:02", 10.0, 4, 8, 32, "run-b"),
        ("j5", "flux", "JPCP", "PM100", "COMPLETED", "2026-07-01T09:00", 5.0, 1, 1, 4, "run-c"),
    ]
    conn.executemany(
        "INSERT INTO hpc_jobs (job_id, scheduler, model, dataset, state, submit_time, "
        "queue_seconds, nodes, gpus, cpus, mlflow_run_id, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(*r, r[4]) for r in rows],  # reuse state slot as updated_at placeholder (non-null)
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── overview (F6 R1) ─────────────────────────────────────────────────────────


def test_overview_allocation_and_queue_depth(platform_db):
    ov = facility.facility_overview(platform_db)
    # running: j1 (2n/4g) + j4 (4n/8g) = 6 nodes / 12 gpus
    assert ov["nodesAllocated"] == 6
    assert ov["gpusAllocated"] == 12
    assert ov["jobsRunning"] == 2
    assert ov["queueDepth"] == 2  # j2, j3
    assert set(ov["clusters"]) == {"slurm", "flux"}


def test_overview_cluster_filter(platform_db):
    ov = facility.facility_overview(platform_db, scheduler="flux")
    assert ov["gpusAllocated"] == 8  # only j4
    assert ov["queueDepth"] == 0


def test_overview_graceful_on_empty(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # no hpc_jobs table
    monkeypatch.setenv("PLATFORM_DB", str(db))
    ov = facility.facility_overview(str(db))
    assert ov["queueDepth"] == 0
    assert ov["clusters"] == []


# ── queue (F6 R2) ─────────────────────────────────────────────────────────────


def test_queue_sorted_by_wait_desc(platform_db):
    q = facility.job_queue(platform_db)
    assert [j["id"] for j in q] == ["j2", "j3"]  # 120s before 45s
    assert q[0]["waitSec"] == 120.0
    assert q[0]["cluster"] == "slurm"


# ── job detail (F6 R2) ───────────────────────────────────────────────────────


def test_job_detail_carries_cost_link(platform_db):
    d = facility.job_detail(platform_db, "j1")
    assert d["resources"] == {"nodes": 2, "gpus": 4, "cpus": 16}
    assert d["mlflowRunId"] == "run-a"


def test_job_detail_none_when_unknown(platform_db):
    assert facility.job_detail(platform_db, "nope") is None


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/facility/overview")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_overview_endpoint(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/facility/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    ov = r.json()["facility"]
    assert ov["gpusAllocated"] == 12


@pytest.mark.asyncio
async def test_queue_endpoint(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/facility/queue", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()["queue"]
    assert body["count"] == 2
    assert body["jobs"][0]["id"] == "j2"


@pytest.mark.asyncio
async def test_job_endpoint_404(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/facility/job/nope", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
