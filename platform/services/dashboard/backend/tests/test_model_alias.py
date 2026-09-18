"""Tests for alias PUT/DELETE and extended versions endpoint."""

import httpx
import pytest
from control_plane_client import ControlPlaneClient

from tests.conftest import ADMIN_PW, VIEWER_PW
from tests.fakes import make_control_plane_transport, make_mlflow_transport

_JPCP_META = {
    "name": "JPCP",
    "task_type": "regression",
    "estimator_class": "sklearn.ensemble.RandomForestRegressor",
    "supported_datasets": ["PM100Dataset"],
    "input_schema": {},
    "output_schema": {},
    "promotion": {
        "metric": "rmse",
        "threshold": 50.0,
        "direction": "lower_is_better",
        "model_id": "jpcp",
    },
    "path_in_repo": "modelzoo/.../jpcp/",
    "bundled_images": [],
}

_VERSIONS = [
    {
        "version": "3",
        "run_id": "r3",
        "aliases": ["Staging"],
        "tags": [{"key": "framework", "value": "sklearn"}],
        "creation_timestamp": 1700000000000,
        "last_updated_timestamp": 1700000001000,
    },
    {
        "version": "2",
        "run_id": "r2",
        "aliases": ["Production"],
        "tags": [{"key": "framework", "value": "sklearn"}],
        "creation_timestamp": 1690000000000,
        "last_updated_timestamp": 1690000001000,
    },
    {
        "version": "1",
        "run_id": "r1",
        "aliases": ["Archived"],
        "tags": [],
        "creation_timestamp": 1680000000000,
        "last_updated_timestamp": 1680000001000,
    },
]


async def _login(client, password: str) -> str:
    r = await client.post("/api/auth/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake_deps(monkeypatch):
    cp_transport = make_control_plane_transport({"JPCP": _JPCP_META})
    mlflow_transport = make_mlflow_transport({"jpcp": _VERSIONS})
    monkeypatch.setattr(
        "routers.models._control_plane",
        lambda: ControlPlaneClient(base_url="http://cp", transport=cp_transport),
    )
    monkeypatch.setattr(
        "routers.models._mlflow_client",
        lambda: httpx.AsyncClient(base_url="http://mlflow", transport=mlflow_transport),
    )


@pytest.mark.asyncio
async def test_versions_includes_all_aliases_and_framework(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/models/JPCP/versions", headers=_hdr(token))
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) == 3
    # Most recent version first
    v3 = next(v for v in versions if v["version"] == "3")
    assert "Staging" in v3["aliases"]
    assert v3["framework"] == "sklearn"
    assert "rmse" in v3["metrics"]


@pytest.mark.asyncio
async def test_set_alias_requires_admin(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_set_production_alias_demotes_previous(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    # Promote v3 to Production (currently v2 is Production)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 200
    versions = r.json()
    v3 = next(v for v in versions if v["version"] == "3")
    v2 = next(v for v in versions if v["version"] == "2")
    assert "Production" in v3["aliases"]
    assert "Production" not in v2["aliases"]
    assert "Archived" in v2["aliases"]


@pytest.mark.asyncio
async def test_set_invalid_alias_rejected(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "NotAnAlias"},
        headers=_hdr(token),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_alias_requires_admin(client, fake_deps):
    token = await _login(client, VIEWER_PW)
    r = await client.delete(
        "/api/models/JPCP/versions/3/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_delete_alias_removes_it(client, fake_deps):
    token = await _login(client, ADMIN_PW)
    r = await client.delete(
        "/api/models/JPCP/versions/3/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 200
    versions = r.json()
    v3 = next(v for v in versions if v["version"] == "3")
    assert "Staging" not in v3["aliases"]


@pytest.mark.asyncio
async def test_delete_alias_wrong_version_returns_409(client, fake_deps):
    """DELETE on a version that doesn't hold the alias must return 409."""
    token = await _login(client, ADMIN_PW)
    # Staging belongs to version 3, not version 1 — expect 409.
    r = await client.delete(
        "/api/models/JPCP/versions/1/alias/Staging",
        headers=_hdr(token),
    )
    assert r.status_code == 409
    detail = r.json().get("detail", "")
    assert "Staging" in detail
    assert "3" in detail  # holder version mentioned in the error


# ─── D1: dashboard promotion goes through the shared eval/calibration gate ────


class _FailingMetric:
    name = "accuracy"
    failed = True


class _FailingGate:
    passed = False
    mode = "block"
    metrics = [_FailingMetric()]


@pytest.mark.asyncio
async def test_promotion_blocked_by_eval_gate(client, fake_deps, monkeypatch):
    """A failing shared gate must block the dashboard Production move (403) and audit it."""
    import routers.models as m

    monkeypatch.setattr(m, "_promotion_gate_outcome", lambda mid, v: (403, "promotion blocked"))
    audited = []
    monkeypatch.setattr(m.audit_write, "audit", lambda *a, **k: audited.append(a))

    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 403
    assert "blocked" in r.json()["detail"]
    assert audited and audited[0][1] == "promotion_blocked_by_gate"


@pytest.mark.asyncio
async def test_promotion_gate_outcome_blocks_on_failing_gate(monkeypatch):
    import routers.models as m

    fake_gate_mod = type("G", (), {"run_eval_gate": staticmethod(lambda *a, **k: _FailingGate())})
    import sys

    monkeypatch.setitem(sys.modules, "examlops.evaluation.gate", fake_gate_mod)
    verdict = m._promotion_gate_outcome("jpcp", "3")
    assert verdict is not None and verdict[0] == 403
    assert "accuracy" in verdict[1]


@pytest.mark.asyncio
async def test_promotion_gate_outcome_blocks_when_gate_cannot_run(monkeypatch):
    """A broken gate must not masquerade as a pass — 503, not silent allow."""
    import sys

    import routers.models as m

    class _Boom:
        @staticmethod
        def run_eval_gate(*a, **k):
            raise RuntimeError("db unreachable")

    monkeypatch.setitem(sys.modules, "examlops.evaluation.gate", _Boom)
    verdict = m._promotion_gate_outcome("jpcp", "3")
    assert verdict is not None and verdict[0] == 503


@pytest.mark.asyncio
async def test_successful_alias_set_is_audited(client, fake_deps, monkeypatch):
    import routers.models as m

    monkeypatch.setattr(m, "_promotion_gate_outcome", lambda mid, v: None)
    audited = []
    monkeypatch.setattr(m.audit_write, "audit", lambda *a, **k: audited.append(a))

    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias",
        json={"alias": "Production"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    assert audited and audited[0][1] == "model_promoted"


@pytest.mark.asyncio
async def test_versions_beyond_the_first_page_are_not_lost(client, fake_deps, monkeypatch):
    """Every version must be returned, however many pages MLflow splits them across.

    MLflow pages `model-versions/search`. Reading only the first page does not merely shorten a
    list: the aliases are resolved from it, so a Production alias pointing at a version that fell
    off the page makes the dashboard report the model as having no production version at all —
    while it is serving one. `examlops.serving_snapshot` already follows the token "by design";
    this path did not.
    """
    from tests.fakes import MLFLOW_VERSION_PAGE_SIZE

    # A fixed, small count that is asserted to span several pages, rather than a multiple of the
    # page size: deriving the count from the constant means raising the constant silently inflates
    # this test instead of failing it (at page size 100k it built 300k versions and took 40 s).
    n = 7
    assert n > MLFLOW_VERSION_PAGE_SIZE * 2, (
        f"the fake's page size ({MLFLOW_VERSION_PAGE_SIZE}) no longer makes {n} versions span "
        "several pages, so this test would pass without the router following any token"
    )
    versions = [
        {
            "version": str(i),
            "run_id": f"r{i}",
            "current_stage": "None",
            # The alias sits on the LAST page — the version an operator most needs to see.
            "aliases": ["Production"] if i == 1 else [],
            "creation_timestamp": 1600000000000 + i,
            "last_updated_timestamp": 1600000000000 + i,
        }
        for i in range(n, 0, -1)
    ]
    transport = make_mlflow_transport({"JPCP": versions})
    monkeypatch.setattr(
        "routers.models._mlflow_client",
        lambda: httpx.AsyncClient(base_url="http://mlflow", transport=transport),
    )

    token = await _login(client, VIEWER_PW)
    body = (await client.get("/api/models/JPCP/versions", headers=_hdr(token))).json()
    assert len(body) == n, body
    prod = [row for row in body if row["alias"] == "Production"]
    assert [row["version"] for row in prod] == ["1"], body


@pytest.mark.asyncio
async def test_a_registry_that_never_stops_paging_is_refused_not_truncated(
    client, fake_deps, monkeypatch
):
    """A page token that repeats must end the request, and must not look like a short answer.

    Returning what had been read so far would be the original bug wearing a loop: a silently
    incomplete version list. Refusing says the registry is misbehaving, which is true and
    actionable; a worker spinning forever on the same page says nothing and costs capacity.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "registered-models/get" in path:
            return httpx.Response(200, json={"registered_model": {"aliases": []}})
        if "model-versions/search" in path:
            return httpx.Response(
                200,
                json={
                    "model_versions": [{"version": "1", "run_id": "r1"}],
                    "next_page_token": "always-the-same",
                },
            )
        if "runs/get" in path:
            return httpx.Response(200, json={"run": {"data": {"metrics": [], "tags": []}}})
        return httpx.Response(404, json={"detail": path})

    monkeypatch.setattr(
        "routers.models._mlflow_client",
        lambda: httpx.AsyncClient(base_url="http://mlflow", transport=httpx.MockTransport(handler)),
    )
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/models/JPCP/versions", headers=_hdr(token))
    assert r.status_code == 502, r.text
    assert "page token" in r.json()["detail"]
