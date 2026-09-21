"""Every mutating control-plane route must consult policy or say why not (ADR 0079 d2).

Same forcing function as the dashboard's ``test_policy_route_table.py``: adding a POST/PUT/PATCH/
DELETE route without classifying it in ``cplane.policy_gate.ROUTE_POLICY`` fails this test.
"""

from __future__ import annotations

import inspect
import re

import app as cp_app
from cplane.policy_gate import ROUTE_POLICY, route_key

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _walk(routes, prefix=""):
    """Best-effort duck-typed walk of ``app.routes`` (nested routers too)."""
    found = set()
    for route in routes:
        path = prefix + str(getattr(route, "path", "") or "")
        found |= {(m, path) for m in (getattr(route, "methods", None) or ()) if m in MUTATING}
        found |= _walk(getattr(route, "routes", None) or (), path)
    return found


def _live() -> set[tuple[str, str]]:
    """Mutating operations from ``app.openapi()`` (public API, same on every FastAPI release),
    plus a best-effort walk of ``app.routes`` for ``include_in_schema=False`` routes — the
    schema's one blind spot."""
    schema = {
        (m.upper(), path)
        for path, ops in cp_app.app.openapi()["paths"].items()
        for m in ops
        if m.upper() in MUTATING
    }
    assert schema, "openapi() lists no mutating operation — the guard would pass vacuously"
    return {
        route_key(m, re.sub(r"\{(\w+):\w+\}", r"{\1}", p))
        for m, p in schema | _walk(cp_app.app.routes)
    }


def test_every_mutating_route_is_classified():
    missing = sorted(_live() - set(ROUTE_POLICY))
    assert not missing, (
        "mutating control-plane routes with no policy classification — add to "
        f"cplane.policy_gate.ROUTE_POLICY as ('gated', action) or ('exempt', reason): {missing}"
    )


def test_no_stale_entries():
    assert not sorted(set(ROUTE_POLICY) - _live())


def test_exemptions_state_a_reason_and_kinds_are_valid():
    for key, (kind, text) in ROUTE_POLICY.items():
        assert kind in {"gated", "exempt"}, key
        assert len(text.split()) >= 1
        if kind == "exempt":
            assert len(text.split()) >= 6, f"{key}: an exemption needs a reason"


def test_gated_routes_call_the_gate_after_the_scope_dependency():
    """`retrain` handlers call the gate in their body, i.e. after `_require_action` has run."""
    for name in ("trigger_retrain", "submit_retrain_v1"):
        src = inspect.getsource(getattr(cp_app, name))
        assert "_policy_gate.enforce_retrain(" in src, name
    for key, (kind, action) in ROUTE_POLICY.items():
        if kind == "gated":
            assert action == "retrain"  # one gated action today; extend the assertion with new ones
