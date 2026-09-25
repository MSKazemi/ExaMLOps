"""Estimated-versus-realized performance read (ADR 0022 decision 2) — `GET /api/drift/perf-estimates`."""

import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    from examlops import platform_db as pdb

    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    pdb.record_perf_estimate("JPCP", "accuracy", estimated=0.9, realized=None, baseline=0.95)
    pdb.record_perf_estimate(
        "JPCP", "accuracy", estimated=0.7, realized=0.6, baseline=0.95, method="nannyml-cbpe"
    )
    pdb.record_perf_estimate("Other", "accuracy", estimated=0.5, realized=0.5, baseline=None)
    return str(db)


async def _hdr(client):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_requires_authentication(client, platform_db):
    assert (await client.get("/api/drift/perf-estimates")).status_code in (401, 403)


async def test_rows_carry_estimate_realized_and_the_gap_newest_first(client, platform_db):
    r = await client.get("/api/drift/perf-estimates?model=JPCP", headers=await _hdr(client))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["model"] for row in rows] == ["JPCP", "JPCP"]
    newest, oldest = rows
    assert newest["method"] == "nannyml-cbpe"
    assert newest["gap"] == pytest.approx(0.1)
    # No realized score yet is "unmeasured", never a zero gap.
    assert oldest["realized"] is None and oldest["gap"] is None


async def test_limit_is_bounded(client, platform_db):
    h = await _hdr(client)
    assert len((await client.get("/api/drift/perf-estimates?limit=1", headers=h)).json()) == 1
    assert (await client.get("/api/drift/perf-estimates?limit=5000", headers=h)).status_code == 422
