"""What `/status` actually returns — pinned, because two surfaces guessed and both guessed wrong.

`exa status` and `examlops.sdk.status()` each read a `production_models` (falling back to `models`)
key out of the control plane's `/status` response and rendered the result as the platform's
production models. No version of that endpoint has ever returned either key: it answers exactly
`services` and `pending_approvals`. This operational view is now a sensitive read and therefore
requires a bearer credential. So the field resolved to an empty list on every real call, the
renderer printed nothing for an empty list, and the screen said — by saying nothing — that no model
was in production. Two unit tests fed a hand-written payload containing the invented key, so both
stayed green while the feature did not exist.

This file measures the payload against the real app instead of imagining it. It is deliberately a
*contract* test, not a health test: it asserts the shape both clients depend on, so that adding a
field is free and removing one is loud.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_CP = Path(__file__).parents[2] / "platform/services/control_plane"
sys.path.insert(0, str(_CP))


@pytest.fixture
def cp_client(tmp_path, monkeypatch):
    """A TestClient over the real control-plane app, with every peer ping made to fail."""
    token = "unit-test-status-3f94a718c59d4c8b"
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", token)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.db"))
    import app as cp
    from fastapi.testclient import TestClient

    # The module can already be imported by another test file, so patch the resolved setting too.
    monkeypatch.setattr(cp, "CONTROL_PLANE_TOKEN", token)
    monkeypatch.setattr(cp, "CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))

    # No sockets. Peer reachability is not what this file is about, and a real ping would measure
    # whichever containers happen to run on the developer's machine. `platform_status()` imports
    # `urllib.request` inside the function, so the patch has to be on the module itself — and note
    # that the suite-wide live-service guard could not have caught a leak here either way: `_ping`
    # wraps its call in a bare `except Exception`, which swallows the guard's AssertionError and
    # reports the service as merely down.
    def refuse(*a, **k):
        raise OSError("no network in this test")

    with patch("urllib.request.urlopen", refuse):
        with TestClient(cp.app) as client:
            client.headers.update({"Authorization": f"Bearer {token}"})
            yield client


def test_status_returns_exactly_the_keys_its_clients_read(cp_client):
    body = cp_client.get("/status").json()
    assert set(body) == {"services", "pending_approvals"}, (
        "`exa status` and `sdk.status()` are written against this shape; if you add a key, teach "
        "them about it here rather than letting one of them read a key that is never sent."
    )


def test_status_does_not_carry_production_models(cp_client):
    """The specific fiction. Both clients read these names; the endpoint sends neither."""
    body = cp_client.get("/status").json()
    assert "production_models" not in body
    assert "models" not in body


def test_status_reports_every_service_its_clients_render(cp_client):
    body = cp_client.get("/status").json()
    from examlops.cli.commands.status import _SERVICE_ORDER

    missing = [k for k in _SERVICE_ORDER if k not in body["services"]]
    assert not missing, f"{missing} are rendered by `exa status` but not reported by /status"


def test_each_service_entry_carries_the_fields_the_table_prints(cp_client):
    for name, svc in cp_client.get("/status").json()["services"].items():
        assert "ok" in svc, f"{name} has no `ok`, and the table defaults a missing one to False"
        assert "url" in svc, f"{name} has no `url`, so the Checked column would print `?`"
