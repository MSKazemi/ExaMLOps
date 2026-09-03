"""Route-binding guard for the Ray Serve FastAPI app.

Exists because of a real outage-class bug: a helper function was inserted between
``@_app.post("/predict/{model_name}")`` and ``def predict``, so the decorator silently bound the
route to the helper — every HTTP inference request then failed response validation while the
method-level unit tests stayed green. These tests assert each route is bound to the function it
names, and that no private helper (``_``-prefixed) ever carries a route.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.routing import APIRoute  # noqa: E402

from serving.ray_serving import app as rs_app  # noqa: E402


def _api_routes() -> list[APIRoute]:
    return [r for r in rs_app._app.routes if isinstance(r, APIRoute)]


def test_predict_route_is_bound_to_predict():
    route = next(r for r in _api_routes() if r.path == "/predict/{model_name}")
    assert route.endpoint.__name__ == "predict", (
        f"/predict/{{model_name}} is bound to {route.endpoint.__name__!r} — a decorator has "
        "become detached from the predict method"
    )
    assert "POST" in route.methods


def test_no_route_is_bound_to_a_private_helper():
    offenders = [
        (r.path, r.endpoint.__name__) for r in _api_routes() if r.endpoint.__name__.startswith("_")
    ]
    assert not offenders, (
        f"routes bound to private helpers (decorator drifted off its function): {offenders}"
    )


def test_core_routes_exist_and_name_their_endpoint():
    """Each core path must exist and be bound to a same-named public method."""
    by_path = {r.path: r for r in _api_routes()}
    for path, expected in {
        "/predict/{model_name}": "predict",
        "/health": None,  # name not contractual, existence is
        "/reload": None,
        "/models": None,
    }.items():
        assert path in by_path, f"route {path} has disappeared from the serving app"
        if expected:
            assert by_path[path].endpoint.__name__ == expected
