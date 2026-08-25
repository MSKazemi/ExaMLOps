"""Tests for ModelZoo integration: DB, webhooks, poller, API."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_WEBHOOK_SECRET", "webhook-secret")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)
    return TestClient(cp_app.app)


def test_modelzoo_tables_created(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    import importlib

    import app as cp_app

    importlib.reload(cp_app)
    conn = cp_app._get_db()
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "modelzoo_events" in tables
    assert "model_freshness" in tables


def test_modelzoo_status_returns_unknown_for_fresh_db(client):
    resp = client.get("/modelzoo/status", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert "last_event" in data
    # With no events, last_event is None
    assert data["last_event"] is None


import hashlib
import hmac as hmaclib
import json as _json


def _github_sig(body: bytes, secret: str) -> str:
    mac = hmaclib.new(secret.encode(), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


_GITLAB_PUSH = {
    "object_kind": "push",
    "ref": "refs/heads/main",
    "commits": [{"id": "aabbccdd" * 5, "message": "add model", "author": {"name": "alice"}}],
    "user_name": "alice",
}

_GITHUB_PUSH = {
    "ref": "refs/heads/main",
    "head_commit": {"id": "bbccddee" * 5, "message": "update model"},
    "pusher": {"name": "bob"},
}


def test_gitlab_webhook_rejects_bad_token(client):
    r = client.post(
        "/webhooks/modelzoo/gitlab",
        json=_GITLAB_PUSH,
        headers={"X-Gitlab-Token": "wrong-secret"},
    )
    assert r.status_code == 401


def test_gitlab_webhook_ignores_non_main_branch(client):
    payload = {**_GITLAB_PUSH, "ref": "refs/heads/feature/x"}
    r = client.post(
        "/webhooks/modelzoo/gitlab",
        json=payload,
        headers={"X-Gitlab-Token": "webhook-secret"},
    )
    assert r.status_code == 200
    assert r.json().get("skipped") is True


def test_gitlab_webhook_records_event_on_main(client):
    r = client.post(
        "/webhooks/modelzoo/gitlab",
        json=_GITLAB_PUSH,
        headers={"X-Gitlab-Token": "webhook-secret"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["event_id"] >= 1
    assert data["models_marked_stale"] >= 0
    assert "retrain_triggered" in data


def test_github_webhook_rejects_bad_signature(client):
    body = _json.dumps(_GITHUB_PUSH).encode()
    r = client.post(
        "/webhooks/modelzoo/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=badhex",
        },
    )
    assert r.status_code == 401


def test_github_webhook_records_event_on_main(client):
    body = _json.dumps(_GITHUB_PUSH).encode()
    sig = _github_sig(body, "webhook-secret")
    r = client.post(
        "/webhooks/modelzoo/github",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["event_id"] >= 1


def test_github_webhook_ignores_non_main_branch(client):
    payload = {**_GITHUB_PUSH, "ref": "refs/heads/feature/x"}
    body = _json.dumps(payload).encode()
    sig = _github_sig(body, "webhook-secret")
    r = client.post(
        "/webhooks/modelzoo/github",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert r.status_code == 200
    assert r.json().get("skipped") is True


from unittest.mock import MagicMock, patch


def _mock_gitlab_commits(sha: str):
    """Return a mock urllib response that looks like GitLab's commits endpoint."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = _json.dumps(
        [{"id": sha, "author_name": "ci-bot", "committed_date": "2026-05-21T10:00:00.000Z"}]
    ).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_run_poll_cycle_inserts_new_event(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "poll_test.db"))
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    new_sha = "deadbeef" * 5

    with patch("urllib.request.urlopen", return_value=_mock_gitlab_commits(new_sha)):
        result = cp_app._run_poll_cycle()

    assert result.get("new") is True
    assert result["commit_sha"] == new_sha

    conn = cp_app._get_db()
    row = conn.execute(
        "SELECT commit_sha, source FROM modelzoo_events WHERE commit_sha = ?", (new_sha,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "poll"


def test_run_poll_cycle_skips_known_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "poll_skip.db"))
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    known_sha = "cafebabe" * 5
    # Seed the event as already known
    conn = cp_app._get_db()
    conn.execute(
        "INSERT INTO modelzoo_events (commit_sha, branch, pushed_by, timestamp, source) "
        "VALUES (?, 'main', 'ci', '2026-05-21T09:00:00', 'poll')",
        (known_sha,),
    )
    conn.commit()
    conn.close()

    with patch("urllib.request.urlopen", return_value=_mock_gitlab_commits(known_sha)):
        result = cp_app._run_poll_cycle()

    assert result == {}


def test_run_poll_cycle_reports_network_error(tmp_path, monkeypatch):
    """A failed GitLab fetch must return an error marker, not an empty/no-op dict."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "poll_err.db"))
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    boom = OSError("Temporary failure in name resolution")
    with patch("urllib.request.urlopen", side_effect=boom):
        result = cp_app._run_poll_cycle()

    assert "error" in result
    assert "name resolution" in result["error"]


def test_modelzoo_sync_surfaces_poll_error(tmp_path, monkeypatch):
    """`/modelzoo/sync` must report the failure reason rather than 'up-to-date'."""
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "sync_err.db"))
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)
    from fastapi.testclient import TestClient

    c = TestClient(cp_app.app)
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        resp = c.post("/modelzoo/sync", headers=_auth(c))
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_commit"] is False
    assert body["error"] == "boom"


def _auth(client):
    return {"Authorization": "Bearer test-token"}


def test_modelzoo_events_empty_list(client):
    resp = client.get("/modelzoo/events", headers=_auth(client))
    assert resp.status_code == 200
    assert resp.json() == []


def test_modelzoo_events_after_webhook(client):
    client.post(
        "/webhooks/modelzoo/gitlab",
        json=_GITLAB_PUSH,
        headers={"X-Gitlab-Token": "webhook-secret"},
    )
    resp = client.get("/modelzoo/events", headers=_auth(client))
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert events[0]["commit_sha"] == _GITLAB_PUSH["commits"][0]["id"]
    assert events[0]["source"] == "webhook"


def test_modelzoo_sync_no_credentials(client, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)
    from fastapi.testclient import TestClient

    c = TestClient(cp_app.app)
    resp = c.post("/modelzoo/sync", headers=_auth(c))
    assert resp.status_code == 200
    assert resp.json()["new_commit"] is False


def test_modelzoo_config_defaults(client):
    resp = client.get("/modelzoo/config", headers=_auth(client))
    assert resp.status_code == 200
    cfg = resp.json()
    assert cfg["auto_retrain"] is False
    assert cfg["poll_interval_seconds"] == 300
    assert cfg["watch_branch"] == "main"


def test_modelzoo_config_update_requires_token(client):
    resp = client.put("/modelzoo/config", json={"auto_retrain": True, "poll_interval_seconds": 60})
    assert resp.status_code in (401, 403)


def test_modelzoo_config_update(client):
    resp = client.put(
        "/modelzoo/config",
        json={"auto_retrain": True, "poll_interval_seconds": 120},
        headers=_auth(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_retrain"] is True
    assert data["poll_interval_seconds"] == 120


# ── CI pipeline trigger tests ─────────────────────────────────────────────────


def _mock_pipeline_trigger_response(pipeline_id: int = 999):
    mock_resp = MagicMock()
    mock_resp.read.return_value = _json.dumps(
        {"id": pipeline_id, "web_url": f"https://gitlab.example.com/pipelines/{pipeline_id}"}
    ).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_trigger_ci_pipeline_skipped_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "trigger_test.db"))
    monkeypatch.delenv("AI_PROD_GITLAB_PROJECT_ID", raising=False)
    monkeypatch.delenv("AI_PROD_PIPELINE_TRIGGER_TOKEN", raising=False)
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    with patch("urllib.request.urlopen") as mock_urlopen:
        result = cp_app._trigger_ci_pipeline("deadbeef" * 5)

    assert result is False
    mock_urlopen.assert_not_called()


def test_trigger_ci_pipeline_calls_gitlab_api(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "trigger_test2.db"))
    monkeypatch.setenv("AI_PROD_GITLAB_PROJECT_ID", "88")
    monkeypatch.setenv("AI_PROD_PIPELINE_TRIGGER_TOKEN", "ci-tok")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    sha = "cafecafe" * 5
    with patch("urllib.request.urlopen", return_value=_mock_pipeline_trigger_response()) as mock_ul:
        result = cp_app._trigger_ci_pipeline(sha)

    assert result is True
    called_url = mock_ul.call_args[0][0].full_url
    assert "projects/88/trigger/pipeline" in called_url


def test_webhook_response_includes_ci_pipeline_triggered(client):
    r = client.post(
        "/webhooks/modelzoo/gitlab",
        json=_GITLAB_PUSH,
        headers={"X-Gitlab-Token": "webhook-secret"},
    )
    assert r.status_code == 200
    assert "ci_pipeline_triggered" in r.json()


def test_poll_cycle_triggers_ci_pipeline_on_new_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "poll_trigger.db"))
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    monkeypatch.setenv("AI_PROD_GITLAB_PROJECT_ID", "88")
    monkeypatch.setenv("AI_PROD_PIPELINE_TRIGGER_TOKEN", "ci-tok")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    import importlib

    import app as cp_app

    importlib.reload(cp_app)

    new_sha = "feedface" * 5
    with patch(
        "urllib.request.urlopen",
        side_effect=[
            _mock_gitlab_commits(new_sha),  # poll: fetch latest commit
            _mock_pipeline_trigger_response(),  # trigger: fire CI pipeline
        ],
    ):
        result = cp_app._run_poll_cycle()

    assert result.get("new") is True
    assert result["commit_sha"] == new_sha
