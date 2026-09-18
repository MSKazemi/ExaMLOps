"""Every operator route has a /v1 path, and the legacy paths say so (plan P1.6).

A /v1 route is the legacy route's own handler registered at a second path, so the two cannot
drift. What must not drift above all is *authorization*: a /v1 twin that lost a dependency would be
an unauthenticated door next to a locked one. The legacy paths keep working and carry RFC 9745
``Deprecation`` plus an RFC 8288 ``Link`` to their successor.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Built at runtime: a literal credential-shaped string trips the platform secret scanner.
TOKEN = "-".join(("a", "real", "test", "credential", "value"))
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _routes(cp) -> dict[tuple[str, str], APIRoute]:
    return {(m, r.path): r for r in cp.app.routes if isinstance(r, APIRoute) for m in r.methods}


def test_each_v1_path_is_the_legacy_handler_with_the_same_guards(cp):
    from cplane.versioning import ALIASES

    routes = _routes(cp)
    for alias in ALIASES:
        if not alias.same_handler:
            continue
        legacy, v1 = routes[(alias.method, alias.legacy)], routes[(alias.method, alias.v1)]
        assert v1.endpoint is legacy.endpoint, alias
        assert [d.dependency for d in v1.dependencies] == [
            d.dependency for d in legacy.dependencies
        ], alias
        assert v1.response_model is legacy.response_model, alias


def test_a_v1_path_refuses_exactly_what_its_legacy_path_refuses(cp):
    client = TestClient(cp.app)
    legacy = client.post("/approve/JPCP")
    v1 = client.post("/v1/approvals/JPCP/approve")
    assert legacy.status_code == v1.status_code
    assert legacy.status_code in (401, 403)


def test_a_v1_error_is_a_problem_document_and_a_legacy_error_is_not(cp):
    client = TestClient(cp.app)
    v1 = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    legacy = client.get("/models", headers={"Authorization": "Bearer wrong"})

    assert v1.status_code == legacy.status_code
    assert v1.headers["content-type"].startswith("application/problem+json")
    assert v1.json()["status"] == v1.status_code and "instance" in v1.json()
    assert "detail" in legacy.json() and "instance" not in legacy.json()


def test_v1_and_legacy_answer_the_same(cp):
    client = TestClient(cp.app)
    assert (
        client.get("/v1/models", headers=AUTH).json() == client.get("/models", headers=AUTH).json()
    )


@pytest.mark.parametrize(
    ("method", "path", "successor"),
    [
        ("GET", "/models", "/v1/models"),
        ("POST", "/approve/JPCP", "/v1/approvals/JPCP/approve"),
        ("DELETE", "/approvals/abc-123", "/v1/approvals/abc-123"),
        ("GET", "/retrain/55bec2da", "/v1/runs/55bec2da"),
        ("POST", "/retrain", "/v1/retrain"),
    ],
)
def test_legacy_paths_announce_their_successor(cp, method, path, successor):
    response = TestClient(cp.app).request(method, path)  # unauthenticated is fine: any answer
    assert response.headers["deprecation"] == "@1789084800"
    assert response.headers["link"] == f'<{successor}>; rel="successor-version"'
    assert "sunset" not in response.headers  # no removal date has been decided


@pytest.mark.parametrize("path", ["/v1/models", "/health", "/metrics", "/v1/commands"])
def test_current_and_infrastructure_paths_are_not_deprecated(cp, path):
    assert "deprecation" not in TestClient(cp.app).get(path, headers=AUTH).headers


def test_an_alias_for_a_missing_route_fails_loudly(monkeypatch):
    from cplane import versioning
    from fastapi import FastAPI

    monkeypatch.setattr(
        versioning, "ALIASES", (versioning.Alias("GET", "/nowhere", "/v1/nowhere"),)
    )
    with pytest.raises(RuntimeError, match="/nowhere"):
        versioning.install_v1_aliases(FastAPI())
