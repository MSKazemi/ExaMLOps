"""Security tests for authenticated agent principals and checkpoint namespaces."""

from __future__ import annotations

import json


def test_distinct_configured_keys_resolve_to_distinct_principals(monkeypatch):
    from skipper import auth, config

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(
        config, "AGENT_API_KEYS_JSON", json.dumps({"alice": "key-a", "bob": "key-b"})
    )
    monkeypatch.setattr(config, "AGENT_TENANT", "production")

    alice = auth.authenticate_bearer("Bearer key-a")
    bob = auth.authenticate_bearer("Bearer key-b")

    assert alice == auth.AgentIdentity("alice", "production")
    assert bob == auth.AgentIdentity("bob", "production")
    assert auth.authenticate_bearer("Bearer wrong") is None
    assert auth.scope_thread_id(alice, "incident") != auth.scope_thread_id(bob, "incident")


def test_cookie_preserves_verified_principal_and_rejects_tampering(monkeypatch):
    from skipper import auth, config

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", json.dumps({"alice": "key-a"}))
    monkeypatch.setattr(config, "AGENT_TENANT", "research")
    monkeypatch.setattr(config, "AGENT_ACTION_SIGNING_KEY", "server-only")
    identity = auth.authenticate_key("key-a")
    assert identity is not None

    cookie = auth.issue_cookie(identity)

    assert auth.authenticate_cookie(cookie) == identity
    assert auth.authenticate_cookie(f"{cookie[:-1]}x") is None
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", json.dumps({"alice": "rotated"}))
    assert auth.authenticate_cookie(cookie) is None


def test_signed_cookie_expires_server_side(monkeypatch):
    from skipper import auth, config

    monkeypatch.setattr(config, "AGENT_API_KEY", "secret")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", "")
    monkeypatch.setattr(config, "AGENT_BROWSER_SESSION_TTL_SECONDS", 10)
    identity = auth.authenticate_key("secret")
    assert identity is not None
    cookie = auth.issue_cookie(identity, now=100)

    monkeypatch.setattr(auth.time, "time", lambda: 109)
    assert auth.authenticate_cookie(cookie) == identity
    monkeypatch.setattr(auth.time, "time", lambda: 110)
    assert auth.authenticate_cookie(cookie) is None


def test_malformed_explicit_credential_map_fails_closed(monkeypatch):
    from skipper import auth, config

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", "not-json")

    assert auth.auth_required() is True
    assert auth.authenticate_bearer(None) is None
    assert auth.authenticate_bearer("Bearer anything") is None


def test_caller_thread_text_cannot_escape_owner_namespace(monkeypatch):
    from skipper import auth, config

    monkeypatch.setattr(config, "AGENT_TENANT", "tenant-a")
    identity = auth.AgentIdentity("alice", "tenant-a")
    forged = "owner:someone-else:rw:private"

    stored = auth.scope_thread_id(identity, forged)

    assert stored.startswith(auth.owner_prefix(identity))
    assert auth.unscoped_thread_id(identity, stored) == forged
    assert auth.unscoped_thread_id(auth.AgentIdentity("bob", "tenant-a"), stored) is None
