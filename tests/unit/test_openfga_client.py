"""OpenFGA HTTP client behind the authz seam (ADR 0014 decision 2), against a fake HTTP server.

The fake speaks the two endpoints the client uses (``/stores/<id>/check`` and ``/write``), stores
tuples, and evaluates ``owner >= editor >= viewer`` plus ``parent`` inheritance - enough to prove the
client's mapping and its fail-closed behaviour against a real socket, not a mock of ``httpx``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from examlops import authz
from examlops.authz import openfga_client as fga

_RANK = {"viewer": 1, "editor": 2, "owner": 3}
STORE = "01STORE"


class FakeFga:
    def __init__(self):
        self.tuples: set[tuple[str, str, str]] = set()
        self.requests: list[tuple[str, dict, dict]] = []
        self.mode = "ok"  # ok | slow | 500 | garbage | badshape
        self.delay = 0.0

    def _rank_on(self, user: str, obj: str, extra: frozenset = frozenset()) -> int:
        tuples = self.tuples | extra  # stored + the request's contextual tuples
        best = max(
            (_RANK[r] for (u, r, o) in tuples if u == user and o == obj and r in _RANK),
            default=0,
        )
        for u, r, o in tuples:
            if r == "parent" and o == obj:  # `u` is the parent object
                best = max(best, self._rank_on(user, u, extra))
        return best

    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.mode == "slow":
                    time.sleep(outer.delay)
                if outer.mode == "500":
                    return self._send(500, {"code": "internal_error"})
                if outer.mode == "garbage":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"<html>not json")
                    return
                if self.path == f"/stores/{STORE}/check":
                    k = body["tuple_key"]
                    if outer.mode == "badshape":
                        return self._send(200, {"allowed": "yes"})
                    ctx = frozenset(
                        (t["user"], t["relation"], t["object"])
                        for t in (body.get("contextual_tuples") or {}).get("tuple_keys", [])
                    )
                    rank = outer._rank_on(k["user"], k["object"], ctx)
                    ok = rank >= _RANK.get(k["relation"], 99)
                    return self._send(200, {"allowed": ok})
                if self.path == f"/stores/{STORE}/write":
                    for kind in ("writes", "deletes"):
                        for k in (body.get(kind) or {}).get("tuple_keys", []):
                            t = (k["user"], k["relation"], k["object"])
                            if kind == "writes":
                                if t in outer.tuples:
                                    return self._send(400, {"message": "tuple already exists"})
                                outer.tuples.add(t)
                            else:
                                if t not in outer.tuples:
                                    return self._send(400, {"message": "tuple does not exist"})
                                outer.tuples.discard(t)
                    return self._send(200, {})
                return self._send(404, {})

            def _send(self, code, doc):
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return H


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = FakeFga()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.setenv("EXAMLOPS_OPENFGA_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("EXAMLOPS_OPENFGA_STORE_ID", STORE)
    monkeypatch.setenv("EXAMLOPS_OPENFGA_TIMEOUT", "0.3")
    fga.reset_config_log()
    from examlops.platform_db import init_db

    init_db()
    yield fake
    srv.shutdown()


def _native(subject, obj):
    from examlops.data.governance import get_relations_for

    return get_relations_for(subject, obj)


def test_grant_writes_tuples_and_check_reads_them(server):
    authz.grant("alice", "editor", "project:acme")
    assert ("user:alice", "editor", "project:acme") in server.tuples
    assert authz.check("alice", "viewer", "project:acme")  # editor >= viewer
    assert authz.check("alice", "editor", "project:acme")
    assert not authz.check("alice", "owner", "project:acme")
    assert not authz.check("mallory", "viewer", "project:acme")


def test_child_object_maps_to_a_parent_tuple_and_inherits(server):
    authz.grant("alice", "owner", "project:acme")
    authz.grant("bob", "viewer", "project:acme/model:JPCP")
    assert ("project:acme", "parent", "model:acme~JPCP") in server.tuples
    assert ("user:bob", "viewer", "model:acme~JPCP") in server.tuples
    assert authz.check("alice", "editor", "project:acme/model:JPCP")  # inherited from the project
    assert authz.check("bob", "viewer", "project:acme/model:JPCP")
    assert not authz.check("bob", "editor", "project:acme/model:JPCP")


def test_a_project_grant_reaches_a_child_nobody_granted_on_directly(server):
    """No `parent` tuple is stored for `model:acme~JPCP` (no grant was ever made on it); the check
    asserts the structural link contextually, so the project editor still reaches its own model,
    and a stranger still does not."""
    authz.grant("alice", "editor", "project:acme")
    assert not any(o == "model:acme~JPCP" for (_, _, o) in server.tuples)
    assert authz.check("alice", "editor", "project:acme/model:JPCP")
    assert authz.check("alice", "viewer", "project:acme/dataset:FData")
    assert not authz.check("alice", "owner", "project:acme/model:JPCP")
    assert not authz.check("mallory", "viewer", "project:acme/model:JPCP")
    body = server.requests[-1][2]
    assert body["contextual_tuples"]["tuple_keys"] == [
        {"user": "project:acme", "relation": "parent", "object": "model:acme~JPCP"}
    ]
    # Nothing was written by a check.
    assert not any(o == "model:acme~JPCP" for (_, _, o) in server.tuples)


def test_a_root_object_check_sends_no_contextual_tuples(server):
    authz.check("alice", "viewer", "project:acme")
    assert "contextual_tuples" not in server.requests[-1][2]


def test_check_consults_openfga_not_the_native_table(server):
    """A native-only grant is invisible once OpenFGA is configured - it is the authority."""
    from examlops.data.governance import grant_relation

    grant_relation("sneaky", "owner", "project:acme", actor="x")
    assert not authz.check("sneaky", "viewer", "project:acme")


def test_revoke_deletes_the_tuple(server):
    authz.grant("alice", "viewer", "project:acme")
    assert authz.check("alice", "viewer", "project:acme")
    authz.revoke("alice", "viewer", "project:acme")
    assert not authz.check("alice", "viewer", "project:acme")
    assert not _native("alice", "project:acme")


def test_regrant_is_idempotent(server):
    authz.grant("alice", "viewer", "project:acme")
    authz.grant("alice", "viewer", "project:acme")  # 400 "already exists" tolerated
    assert authz.check("alice", "viewer", "project:acme")


def test_timeout_is_a_deny(server):
    authz.grant("alice", "owner", "project:acme")
    server.mode, server.delay = "slow", 1.5
    t0 = time.monotonic()
    assert authz.check("alice", "viewer", "project:acme") is False
    assert time.monotonic() - t0 < 1.4  # gave up at the timeout, did not wait out the server


@pytest.mark.parametrize("mode", ["500", "garbage", "badshape"])
def test_server_failure_denies_and_audits_the_error(server, mode):
    authz.grant("alice", "owner", "project:acme")
    server.mode = mode
    assert authz.check("alice", "viewer", "project:acme") is False
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "authz_error" for e in export_audit_events())


def test_connection_refused_denies(server, monkeypatch):
    authz.grant("alice", "owner", "project:acme")
    monkeypatch.setenv("EXAMLOPS_OPENFGA_URL", "http://127.0.0.1:1")  # nothing listens
    assert authz.check("alice", "viewer", "project:acme") is False


def test_grant_refused_by_openfga_is_not_applied_natively(server):
    server.mode = "500"
    with pytest.raises(fga.OpenFgaError):
        authz.grant("alice", "owner", "project:acme")
    assert not _native("alice", "project:acme")


def test_revoke_refused_by_openfga_leaves_the_native_grant(server):
    authz.grant("alice", "owner", "project:acme")
    server.mode = "500"
    with pytest.raises(fga.OpenFgaError):
        authz.revoke("alice", "owner", "project:acme")
    assert _native("alice", "project:acme") == ["owner"]


def test_authorization_model_id_and_token_are_sent(server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OPENFGA_MODEL_ID", "01MODEL")
    monkeypatch.setenv("EXAMLOPS_OPENFGA_TOKEN", "s3cret")
    authz.check("alice", "viewer", "project:acme")
    _path, headers, body = server.requests[-1]
    assert body["authorization_model_id"] == "01MODEL"
    assert headers["Authorization"] == "Bearer s3cret"


def test_unconfigured_uses_native_silently(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.delenv("EXAMLOPS_OPENFGA_URL", raising=False)
    from examlops.platform_db import init_db

    init_db()
    authz.grant("alice", "viewer", "project:acme")
    with caplog.at_level("ERROR"):
        assert authz.check("alice", "viewer", "project:acme")
    assert "OpenFGA" not in caplog.text


def test_half_configured_falls_back_to_native_with_a_loud_log(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.setenv("EXAMLOPS_OPENFGA_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("EXAMLOPS_OPENFGA_STORE_ID", raising=False)
    fga.reset_config_log()
    from examlops.platform_db import init_db

    init_db()
    authz.grant("alice", "viewer", "project:acme")
    with caplog.at_level("ERROR"):
        assert authz.check("alice", "viewer", "project:acme")  # answered natively, no network
    assert "STORE_ID" in caplog.text and "falls back to the native" in caplog.text


def test_single_tenant_mode_never_calls_openfga(server, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY")
    n = len(server.requests)
    assert authz.check("anyone", "owner", "project:acme") is True
    assert len(server.requests) == n


def test_backfill_from_the_native_table(server, monkeypatch):
    from examlops.data.governance import grant_relation

    grant_relation("alice", "owner", "project:acme", actor="x")
    grant_relation("bob", "viewer", "project:acme/model:JPCP", actor="x")
    cfg = fga.config_from_env()
    assert fga.sync_native_grants(cfg, dry_run=True) == {"dry_run": True, "grants": 2}
    assert not server.tuples
    fga.sync_native_grants(cfg, dry_run=False)
    fga.sync_native_grants(cfg, dry_run=False)  # idempotent
    assert authz.check("alice", "owner", "project:acme")
    assert authz.check("bob", "viewer", "project:acme/model:JPCP")


def test_project_delete_drops_openfga_tuples(server):
    from examlops.data.projects import create_project, delete_project

    create_project("acme", created_by="alice")
    authz.grant("alice", "owner", "project:acme")
    assert authz.check("alice", "owner", "project:acme")
    assert delete_project("acme")
    assert not authz.check("alice", "viewer", "project:acme")  # no resurrection via OpenFGA
