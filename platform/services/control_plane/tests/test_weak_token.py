"""Control plane rejects known-placeholder tokens (enterprise-readiness Phase 0, item 0.8 / QW6).

An unset token already fails closed (503). A *weak* placeholder like ``changeme`` is just as
dangerous — it ships in examples and compose files — so it must be treated as unconfigured too.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _reload_with_token(monkeypatch, tmp_path, token: str):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", token)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


@pytest.mark.parametrize("weak", ["changeme", "CHANGEME", "change-me", "placeholder", "todo", ""])
def test_weak_or_unset_token_is_unusable(monkeypatch, tmp_path, weak):
    cp = _reload_with_token(monkeypatch, tmp_path, weak)
    assert cp._token_is_usable() is False


def test_real_token_is_usable(monkeypatch, tmp_path):
    cp = _reload_with_token(monkeypatch, tmp_path, "a-genuinely-set-secret-token")
    assert cp._token_is_usable() is True


def test_protected_endpoint_503_with_weak_token(monkeypatch, tmp_path):
    """A write endpoint must fail closed (503) when only a placeholder token is configured."""
    from fastapi.testclient import TestClient

    cp = _reload_with_token(monkeypatch, tmp_path, "changeme")
    client = TestClient(cp.app)
    # /retrain is token-gated; a placeholder token ⇒ "not configured" ⇒ 503 (before any auth check).
    resp = client.post("/retrain", json={"model_name": "JPCP", "dataset_name": "PM100Dataset"})
    assert resp.status_code == 503
