"""LLMOps console aggregators + /api/v1/llmops/overview (F10 / ADR 0064)."""

import dbconn
import llmops
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.execute(
        "INSERT INTO llm_endpoints (model, engine, hf_model_id, max_model_len, tensor_parallel_size, "
        "dtype, enabled) VALUES ('llama3', 'vllm', 'meta-llama/Llama-3-8B', 8192, 2, 'bfloat16', 1)"
    )
    # two runs for llama3; the newer (id 2) is the one summarized
    conn.execute(
        "INSERT INTO eval_runs (id, model, suite, status) VALUES (1, 'llama3', 'mmlu', 'complete')"
    )
    conn.execute(
        "INSERT INTO eval_runs (id, model, suite, status) VALUES (2, 'llama3', 'mmlu', 'complete')"
    )
    conn.executemany(
        "INSERT INTO eval_results (eval_run_id, metric, value, baseline, passed) VALUES (?,?,?,?,?)",
        [
            (2, "accuracy", 0.82, 0.80, 1),
            (2, "toxicity", 0.15, 0.10, 0),  # regressed → not passed
            (1, "accuracy", 0.70, 0.80, 0),  # from the OLD run — must be ignored
        ],
    )
    conn.commit()
    conn.close()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── endpoint registry (F10 R2) ───────────────────────────────────────────────


def test_endpoints_registry(platform_db):
    out = llmops.endpoints(platform_db)
    e = out["rows"][0]
    assert e["model"] == "llama3"
    assert e["engine"] == "vllm"
    assert e["hfModelId"] == "meta-llama/Llama-3-8B"
    assert e["tensorParallel"] == 2
    assert e["enabled"] is True


def test_endpoints_graceful_without_table(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    dbconn.connect(db, row_factory=None).close()
    assert llmops.endpoints(str(db)) == {"rows": [], "count": 0}


# ── eval summary (F10 R1 / C2) ───────────────────────────────────────────────


def test_eval_summary_uses_latest_run(platform_db):
    out = llmops.eval_summary(platform_db)
    m = out["models"][0]
    assert m["model"] == "llama3"
    assert m["status"] == "complete"
    # only the newest run's 2 metrics — the stale run's metric is excluded
    metrics = {x["metric"]: x for x in m["metrics"]}
    assert set(metrics) == {"accuracy", "toxicity"}
    assert metrics["accuracy"]["passed"] is True
    assert metrics["toxicity"]["passed"] is False
    assert m["passRate"] == 0.5  # 1 of 2 passed


def test_eval_summary_graceful_without_table(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    dbconn.connect(db, row_factory=None).close()
    assert llmops.eval_summary(str(db)) == {"models": [], "count": 0}


# ── endpoint ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llmops_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/llmops/overview")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_llmops_endpoint_composes(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/llmops/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["endpoints"]["rows"][0]["model"] == "llama3"
    assert body["evals"]["models"][0]["passRate"] == 0.5
