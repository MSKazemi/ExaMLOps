"""Ray Serve's admin routes need a credential; its inference routes never do (plan P0.6 / S1, S3).

``/reload``, ``/reload/{model}`` and ``/infer-pipeline/traffic-rules/{model}`` were anonymous on a
port published to every interface: anyone who could reach it could reroute production traffic.
The guard below reads the *live* route table of both FastAPI apps, so a new admin route added
without the dependency — or the dependency drifting onto ``/predict`` — fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.credentials import bearer_matches, is_usable_secret  # noqa: E402
from serving import admin_auth  # noqa: E402
from serving.inference_pipeline import app as ip_app  # noqa: E402
from serving.ray_serving import app as rs_app  # noqa: E402

GOOD = "s3rv1ng-admin-9f2c4b7e1d"

ADMIN_ROUTES = {
    ("ray", "POST", "/reload"),
    ("ray", "POST", "/reload/{model_name}"),
    ("pipeline", "POST", "/traffic-rules/{model}"),
}
INFERENCE_ROUTES = {
    ("ray", "POST", "/predict/{model_name}"),
    # Open Inference Protocol v2 (plan P4.5): the same inference, in the standard protocol.
    ("ray", "POST", "/v2/models/{model_name}/infer"),
    ("ray", "POST", "/v2/models/{model_name}/versions/{version}/infer"),
    ("pipeline", "POST", "/infer"),
}


def _routes():
    for label, app in (("ray", rs_app._app), ("pipeline", ip_app._ingress_app)):
        for route in app.routes:
            if isinstance(route, APIRoute):
                for method in route.methods:
                    yield (label, method, route.path), route


def _guarded(route: APIRoute) -> bool:
    return any(d.call is admin_auth.require_serving_admin for d in route.dependant.dependencies)


def test_every_admin_route_requires_the_serving_admin_credential():
    table = dict(_routes())
    for key in ADMIN_ROUTES:
        assert key in table, f"admin route {key} disappeared — update ADMIN_ROUTES"
        assert _guarded(table[key]), f"{key} is reachable without RAY_SERVE_ADMIN_TOKEN"


def test_inference_routes_never_require_it():
    table = dict(_routes())
    for key in INFERENCE_ROUTES:
        assert key in table, f"inference route {key} disappeared — update INFERENCE_ROUTES"
        assert not _guarded(table[key]), f"{key} must stay open to inference clients"


def test_no_unclassified_mutating_route():
    """A new POST/PUT/DELETE on serving must be classified as admin or inference, deliberately."""
    unclassified = sorted(
        key
        for key, _route in _routes()
        if key[1] in {"POST", "PUT", "DELETE", "PATCH"}
        and key not in ADMIN_ROUTES
        and key not in INFERENCE_ROUTES
    )
    assert not unclassified, f"classify these serving routes: {unclassified}"


# ─── the dependency itself ─────────────────────────────────────────────────────


@pytest.fixture
def admin_client():
    app = FastAPI()

    @app.post("/admin", dependencies=[Depends(admin_auth.require_serving_admin)])
    def _admin() -> dict:
        return {"ok": True}

    return TestClient(app)


def test_unset_token_closes_the_routes(admin_client, monkeypatch):
    monkeypatch.delenv("RAY_SERVE_ADMIN_TOKEN", raising=False)
    response = admin_client.post("/admin", headers={"Authorization": f"Bearer {GOOD}"})
    assert response.status_code == 503
    assert "RAY_SERVE_ADMIN_TOKEN" in response.json()["detail"]


def test_a_placeholder_token_is_treated_as_unset(admin_client, monkeypatch):
    monkeypatch.setenv("RAY_SERVE_ADMIN_TOKEN", "change-me-serving-token")
    headers = {"Authorization": "Bearer change-me-serving-token"}
    assert admin_client.post("/admin", headers=headers).status_code == 503


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong-but-long-enough"}, 403),
        ({"Authorization": f"Bearer {GOOD}"}, 200),
    ],
)
def test_the_bearer_decides(admin_client, monkeypatch, headers, expected):
    monkeypatch.setenv("RAY_SERVE_ADMIN_TOKEN", GOOD)
    assert admin_client.post("/admin", headers=headers).status_code == expected


def test_callers_send_the_configured_token(monkeypatch):
    monkeypatch.setenv("RAY_SERVE_ADMIN_TOKEN", GOOD)
    assert admin_auth.admin_headers() == {"Authorization": f"Bearer {GOOD}"}
    monkeypatch.delenv("RAY_SERVE_ADMIN_TOKEN")
    assert admin_auth.admin_headers() == {}


# ─── shared secret hygiene ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["", "   ", "changeme", "change-me-control-plane-token", "short", "your-token-here", None],
)
def test_unusable_secrets(value):
    assert not is_usable_secret(value)


def test_a_real_secret_is_usable():
    assert is_usable_secret(GOOD)


def test_bearer_matching_is_exact():
    assert bearer_matches(f"Bearer {GOOD}", GOOD)
    assert not bearer_matches(GOOD, GOOD)  # scheme required
    assert not bearer_matches(f"Bearer {GOOD}x", GOOD)
    assert not bearer_matches(None, GOOD)


def test_a_secret_read_with_surrounding_whitespace_still_matches():
    """A secret from a file or env var often ends in a newline. It was judged usable (stripped)
    and then compared raw, so the correct bearer got 403 (found by the dataplane review)."""
    assert is_usable_secret(f"{GOOD}\n")
    assert bearer_matches(f"Bearer {GOOD}", f"{GOOD}\n")
    assert bearer_matches(f"Bearer {GOOD}", f"  {GOOD} ")


def test_an_empty_token_never_matches_even_an_empty_secret():
    assert not bearer_matches("Bearer ", "")
    assert not bearer_matches("Bearer    ", "  ")


# ─── verify-before-load on the Ray load path ───────────────────────────────────


def test_verification_off_loads_the_registry_uri_unchanged(monkeypatch):
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "off")
    assert rs_app._verified_uri("jpcp", "3", "models:/jpcp@Production") == "models:/jpcp@Production"


def test_enforce_refuses_an_artifact_that_fails_verification(tmp_path, monkeypatch):
    (tmp_path / "model.pkl").write_bytes(b"pickle")
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "enforce")
    monkeypatch.setattr(rs_app.mlflow.artifacts, "download_artifacts", lambda **_k: str(tmp_path))
    monkeypatch.setattr("examlops.supplychain.verify_before_load", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="signature verification failed"):
        rs_app._verified_uri("jpcp", "3", "models:/jpcp@Production")


def test_enforce_loads_exactly_the_verified_local_copy(tmp_path, monkeypatch):
    (tmp_path / "model.pkl").write_bytes(b"pickle")
    seen: dict = {}
    extra: dict = {}

    def _verify(name, version, paths, *, mode, root=None, record=None):
        seen.update(name=name, version=version, paths=[p.name for p in paths], mode=mode)
        # Relative paths are part of what an Ed25519 signature covers (P4.10): the bundle root
        # must be the directory that is loaded.
        extra.update(root=root, record=record)
        return True

    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "enforce")
    monkeypatch.setattr(rs_app.mlflow.artifacts, "download_artifacts", lambda **_k: str(tmp_path))
    monkeypatch.setattr("examlops.supplychain.verify_before_load", _verify)

    assert rs_app._verified_uri("jpcp", "3", "models:/jpcp@Production") == str(tmp_path)
    assert extra["root"] == tmp_path and extra["record"] is None
    assert seen == {"name": "jpcp", "version": "3", "paths": ["model.pkl"], "mode": "enforce"}


def test_a_signature_recorded_under_the_registry_name_is_found_by_serving():
    from examlops.data.registry import get_model_signature
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO model_signatures (model, version, digest, algo, signature, signed_by, "
            "signed_at) VALUES ('JPCP', '3', 'd', 'hmac-sha256', 's', 'op', CURRENT_TIMESTAMP)"
        )
    row = get_model_signature("jpcp", "3")
    assert row is not None and row["digest"] == "d"
