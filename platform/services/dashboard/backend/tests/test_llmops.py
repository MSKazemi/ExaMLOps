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


# ── calculation methodology (ADR 0083) ───────────────────────────────────────


@pytest.fixture
def isolated_provider_config(tmp_path, monkeypatch):
    from examlops.providers import loader

    monkeypatch.setattr(loader, "PROVIDERS_YAML", tmp_path / "providers.yaml")
    monkeypatch.setattr(loader, "FINOPS_YAML", tmp_path / "finops.yaml")
    for d in ("LLM_COST", "LLM_CACHE", "LLM_ROUTING", "RAG_QUALITY"):
        monkeypatch.delenv(f"EXAMLOPS_{d}_PROVIDER", raising=False)
    return tmp_path


def test_calculation_providers_report_the_active_formula(isolated_provider_config, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_LLM_ROUTING_PROVIDER", "cost-latency")
    out = llmops.calculation_providers()
    assert out["available"] is True and out["count"] == 4
    by = {r["domain"]: r for r in out["rows"]}
    # nothing selected for llm_cost ⇒ the gateway's own rate table runs, not `token-rate`
    assert by["llm_cost"]["provider"] is None and by["llm_cost"]["mode"] == "builtin"
    assert by["llm_cost"]["selected"] is False
    assert "rate table" in by["llm_cost"]["methodology"]
    assert by["llm_routing"]["provider"] == "cost-latency" and by["llm_routing"]["selected"]
    assert "latency_weight" in by["llm_routing"]["methodology"]


def test_calculation_providers_degrade_without_the_library(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "examlops.llmops_providers":
            raise ImportError("slim image")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert llmops.calculation_providers() == {"rows": [], "count": 0, "available": False}


@pytest.mark.asyncio
async def test_llmops_endpoint_surfaces_calculations(client, platform_db, isolated_provider_config):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/llmops/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    calc = r.json()["calculations"]
    assert [row["domain"] for row in calc["rows"]] == [
        "llm_cost",
        "llm_cache",
        "llm_routing",
        "rag_quality",
    ]
    assert all(row["methodology"] for row in calc["rows"])
