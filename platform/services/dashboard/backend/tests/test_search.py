"""Federated global search + /api/v1/search endpoint (F2 / ADR 0056)."""

import dbconn
import pytest
import search as search_lib

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.execute(
        "INSERT INTO promotion_rules (model, metric, operator, threshold, from_alias, "
        "to_alias, enabled) VALUES ('jpcp','rmse','<',5.0,'Staging','Production',1)"
    )
    conn.execute(
        "INSERT INTO hpc_jobs (job_id, scheduler, model, dataset, state, updated_at) "
        "VALUES ('slurm-42','slurm','JPCP','PM100','RUNNING','2026-07-02')"
    )
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, ts) "
        "VALUES ('exa-retrain','mohsen','retrain_triggered','JPCP','2026-07-02')"
    )
    conn.commit()
    conn.close()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── scoring (F2 R3 ranking) ──────────────────────────────────────────────────


def test_score_ordering():
    assert search_lib.score("jpcp", "jpcp") == 100  # exact
    assert search_lib.score("jp", "jpcp") == 80  # prefix
    assert search_lib.score("cons", "MLOps Console") == 60  # word-boundary
    assert search_lib.score("lops", "MLOps Console") == 40  # substring
    assert search_lib.score("jcp", "jpcp") == 20  # fuzzy subsequence
    assert search_lib.score("zzz", "jpcp") == 0  # no match


# ── federated search ─────────────────────────────────────────────────────────


def test_blank_query_returns_nothing(platform_db):
    out = search_lib.search(platform_db, "   ")
    assert out["count"] == 0
    assert out["results"] == []


def test_search_federates_and_ranks(platform_db):
    out = search_lib.search(platform_db, "jpcp")
    kinds = {r["kind"] for r in out["results"]}
    # model (exact) + job (model match) + audit (target match)
    assert "model" in kinds
    assert "job" in kinds
    assert "audit" in kinds
    # exact model match ranks first
    assert out["results"][0]["kind"] == "model"
    assert out["results"][0]["url"] == "/models/jpcp"


def test_search_groups_by_source(platform_db):
    out = search_lib.search(platform_db, "jpcp")
    assert "mlflow" in out["groups"]
    assert "scheduler" in out["groups"]


def test_search_pages_need_no_db(platform_db):
    out = search_lib.search(platform_db, "facility")
    pages = [r for r in out["results"] if r["kind"] == "page"]
    assert any(p["url"] == "/facility" for p in pages)


def test_search_graceful_without_tables(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    dbconn.connect(db, row_factory=None).close()
    out = search_lib.search(str(db), "models")
    # pages still match even with no platform tables
    assert any(r["kind"] == "page" for r in out["results"])


# ── endpoint ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/search?q=jpcp")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_search_endpoint(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/search?q=jpcp", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()["search"]
    assert body["count"] > 0
    assert body["results"][0]["kind"] == "model"
