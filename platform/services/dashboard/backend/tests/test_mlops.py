"""MLOps console aggregators + /api/v1/mlops endpoints (F9 / ADR 0060)."""

import dbconn
import mlops
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """A seeded platform.db, wired into the mlops router via PLATFORM_DB."""
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    # jpcp: fully wired (drift + enabled policy + cost + traffic) → ok/governed
    conn.executemany(
        "INSERT INTO drift_snapshots (ts, model, alias, prediction) VALUES (?,?,?,?)",
        [
            ("2026-07-01T10:00:00", "jpcp", "Production", 1.0),
            ("2026-07-02T10:00:00", "jpcp", "Production", 3.0),
        ],
    )
    conn.execute(
        "INSERT INTO model_costs (model_name, version, gpu_hours, cost_usd, recorded_at) "
        "VALUES (?,?,?,?,?)",
        ("jpcp", 18, 4.5, 12.25, "2026-07-02T11:00:00"),
    )
    conn.execute(
        "INSERT INTO traffic_rules (model, rules, updated_at) VALUES (?,?,?)",
        ("jpcp", '{"Production": 90, "Canary": 10}', "2026-07-02T12:00:00"),
    )
    conn.execute(
        "INSERT INTO promotion_rules (model, metric, operator, threshold, from_alias, "
        "to_alias, enabled) VALUES (?,?,?,?,?,?,?)",
        ("jpcp", "rmse", "<", 5.0, "Staging", "Production", 1),
    )
    # demoad: drift only, no policy → warn / ungoverned
    conn.execute(
        "INSERT INTO drift_snapshots (ts, model, alias, prediction) VALUES (?,?,?,?)",
        ("2026-07-01T09:00:00", "demoad", "Staging", 0.5),
    )
    conn.commit()
    conn.close()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── name-casing (F9 R2) ──────────────────────────────────────────────────────


def test_name_casing_is_central():
    assert mlops.display_name("jpcp") == "JPCP"
    assert mlops.mlflow_name("JPCP") == "jpcp"


# ── registry rows (F9 R1) ────────────────────────────────────────────────────


def test_registry_rows_compose_union(platform_db):
    rows = mlops.registry_rows(platform_db)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"JPCP", "DEMOAD"}

    jpcp = by_name["JPCP"]
    assert jpcp["mlflowName"] == "jpcp"
    assert jpcp["version"] == 18
    assert jpcp["stage"] == "Production"
    assert jpcp["health"] == "ok"
    assert jpcp["governed"] is True

    demoad = by_name["DEMOAD"]
    assert demoad["health"] == "warn"  # drift but no policy
    assert demoad["governed"] is False


# ── promotion check (F9 R4) ──────────────────────────────────────────────────


def test_promotion_allowed_with_enabled_policy(platform_db):
    chk = mlops.promotion_check(platform_db, "JPCP")
    assert chk["allowed"] is True
    assert chk["policy"]["allow"] is True
    assert chk["policy"]["metric"] == "rmse"
    assert chk["approval"]["required"] is True  # human step still shown


def test_promotion_denied_with_reason_when_no_policy(platform_db):
    chk = mlops.promotion_check(platform_db, "DEMOAD")
    assert chk["allowed"] is False
    assert "no promotion policy configured" in chk["policy"]["reasons"]


def test_promotion_denied_when_policy_disabled(platform_db):
    conn = dbconn.connect(platform_db, row_factory=None)
    conn.execute("UPDATE promotion_rules SET enabled=0 WHERE model='jpcp'")
    conn.commit()
    conn.close()
    chk = mlops.promotion_check(platform_db, "jpcp")
    assert chk["allowed"] is False
    assert "promotion policy is disabled" in chk["policy"]["reasons"]


# ── model detail tabs (F9 R2) ────────────────────────────────────────────────


def test_model_detail_composes_tabs(platform_db):
    d = mlops.model_detail(platform_db, "jpcp")
    assert d["name"] == "JPCP"
    assert d["cost"]["runs"] == 1
    assert d["cost"]["gpu_hours"] == 4.5
    assert d["drift"]["samples"] == 2
    assert d["drift"]["mean_prediction"] == 2.0
    assert d["traffic"]["configured"] is True
    assert d["promotion"]["allowed"] is True


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/mlops/registry")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_registry_endpoint_returns_rows(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/mlops/registry", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()["registry"]
    assert body["count"] == 2
    assert {row["name"] for row in body["rows"]} == {"JPCP", "DEMOAD"}


@pytest.mark.asyncio
async def test_promotion_endpoint_denies_with_reasons(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/mlops/promotion/DEMOAD", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    chk = r.json()["promotion"]
    assert chk["allowed"] is False
    assert chk["policy"]["reasons"]


@pytest.mark.asyncio
async def test_model_detail_endpoint(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/mlops/model/jpcp", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    detail = r.json()["detail"]
    assert detail["name"] == "JPCP"
    assert detail["drift"]["samples"] == 2
