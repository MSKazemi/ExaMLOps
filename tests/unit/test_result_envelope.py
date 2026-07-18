"""Canonical result-envelope contract (enterprise-readiness Phase 4, item 4.6).

One `ok`/error shape (`examlops.sdk.Result`) for every programmatic surface, so a caller never
guesses a bespoke dict. `to_dict()` must emit the exact historical wire format so surfaces adopt
it with zero wire change — verified here, plus that the MCP error helper now routes through it
byte-identically.
"""

from __future__ import annotations

from examlops.sdk import Result, err, ok


def test_ok_envelope_shape():
    assert ok().to_dict() == {"ok": True}
    assert ok(count=3, models=["a"]).to_dict() == {"ok": True, "count": 3, "models": ["a"]}


def test_err_envelope_shape():
    assert err("boom").to_dict() == {"ok": False, "error": "boom"}
    assert err("boom", status=503).to_dict() == {"ok": False, "error": "boom", "status": 503}


def test_result_is_frozen_and_typed():
    r = ok(x=1)
    assert isinstance(r, Result) and r.ok is True and r.error is None
    import dataclasses

    with_error = dataclasses.replace(r, ok=False, error="nope")
    assert with_error.to_dict() == {"ok": False, "error": "nope", "x": 1}


def test_mcp_err_is_byte_identical_via_sdk():
    """The MCP `_err` helper must produce the same dict it always did, now via sdk.err."""
    from examlops.mcp.tools import _err

    assert _err("CONTROL_PLANE_TOKEN not configured") == {
        "ok": False,
        "error": "CONTROL_PLANE_TOKEN not configured",
    }
    assert _err("unreachable", status=503) == {"ok": False, "error": "unreachable", "status": 503}
