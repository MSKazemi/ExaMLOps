"""Facility console aggregators + /api/v1/facility endpoints (F6 / ADR 0059)."""

import sqlite3

import facility
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


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


# ── fleet registry + approval gate (Phase 35b) ───────────────────────────────


@pytest.fixture
def fleet_db(platform_db):
    """Augment the seeded platform.db with an hpc_clusters registry table."""
    conn = sqlite3.connect(platform_db)
    conn.executescript(
        """
        CREATE TABLE hpc_clusters (
            name TEXT PRIMARY KEY, scheduler TEXT, transport TEXT, host TEXT,
            ssh_user TEXT, ssh_port INTEGER, ssh_key TEXT, key_fingerprint TEXT,
            state TEXT NOT NULL DEFAULT 'PENDING', capabilities TEXT,
            requested_by TEXT, approved_by TEXT, reason TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT, actor TEXT, action TEXT, target TEXT, details TEXT
        );
        INSERT INTO hpc_clusters (name, scheduler, transport, host, state, capabilities)
        VALUES ('lxp', 'flux', 'ssh', 'lxp-login', 'PENDING', '{"total_gpus": 8}');
        """
    )
    conn.commit()
    conn.close()
    return platform_db


@pytest.mark.asyncio
async def test_fleet_lists_clusters(client, fleet_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/facility/fleet", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    c = body["clusters"][0]
    assert c["name"] == "lxp" and c["state"] == "PENDING"
    assert c["capabilities"]["total_gpus"] == 8
    # No node snapshot → capacity falls back to declared capabilities (idle == total).
    assert c["totalGpus"] == 8 and c["idleGpus"] == 8 and c["utilizationPct"] == 0.0


@pytest.mark.asyncio
async def test_fleet_capacity_from_node_snapshot_and_jobs(client, fleet_db):
    # Seed a live node snapshot (half the GPUs allocated) + a completed GPU job.
    conn = sqlite3.connect(fleet_db)
    conn.executescript(
        """
        CREATE TABLE hpc_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, cluster TEXT, scheduler TEXT, node TEXT,
            cpus INTEGER, memory_mb INTEGER, gpus INTEGER, gpu_model TEXT, state TEXT,
            partition TEXT, captured_at TEXT
        );
        INSERT INTO hpc_nodes (cluster, scheduler, node, gpus, state) VALUES
            ('lxp', 'flux', 'n1', 4, 'idle'),
            ('lxp', 'flux', 'n2', 4, 'allocated');
        UPDATE hpc_jobs SET run_seconds = 3600 WHERE scheduler = 'flux' AND gpus = 8;
        """
    )
    conn.commit()
    conn.close()

    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/facility/fleet", headers={"Authorization": f"Bearer {token}"})
    c = r.json()["clusters"][0]
    assert c["totalGpus"] == 8 and c["idleGpus"] == 4  # from the snapshot, not capabilities
    assert c["utilizationPct"] == 50.0
    assert c["gpuHoursUsed"] == 8.0  # 8 GPUs × 3600s / 3600


@pytest.mark.asyncio
async def test_fleet_approve_requires_admin(client, fleet_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/facility/fleet/lxp/approve", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_fleet_admin_approve_flips_state_and_audits(client, fleet_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/facility/fleet/lxp/approve", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert r.json()["state"] == "ACTIVE"

    conn = sqlite3.connect(fleet_db)
    state = conn.execute("SELECT state FROM hpc_clusters WHERE name='lxp'").fetchone()[0]
    audit = conn.execute(
        "SELECT action, target FROM audit_events WHERE action='cluster_approved'"
    ).fetchone()
    conn.close()
    assert state == "ACTIVE"
    assert audit == ("cluster_approved", "lxp")


@pytest.mark.asyncio
async def test_fleet_reject_with_reason(client, fleet_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/facility/fleet/lxp/reject",
        json={"reason": "wrong account"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "REJECTED"

    conn = sqlite3.connect(fleet_db)
    row = conn.execute("SELECT state, reason FROM hpc_clusters WHERE name='lxp'").fetchone()
    conn.close()
    assert row == ("REJECTED", "wrong account")


@pytest.mark.asyncio
async def test_fleet_approve_unknown_404(client, fleet_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/facility/fleet/ghost/approve", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 404
