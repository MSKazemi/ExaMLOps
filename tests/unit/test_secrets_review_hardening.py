"""ADR 0011 review hardening — each test here failed against the first implementation.

* A vault path is resolved by a Go server that *cleans* it (``acme/../globex/key`` and
  ``/globex/key`` are redirected to ``globex/key``), so the path-prefix tenant check could be
  walked around. The fake below reproduces that redirect rather than assuming it away.
* A write to a store every tenant shares (vault / SOPS) of an unprefixed path replaced that shared
  secret for every tenant; under multitenancy only ``admin`` may do that.
* ``lease renew`` / ``lease revoke`` had no tenant check, so one tenant could cut off another's
  running job.
* ``EXAMLOPS_SECRETS_INJECT=0`` left ``secret://…`` literals in the environment, where a token
  check would accept that public string as the credential.
* The control plane injected before it pinned ``PLATFORM_DB`` to ``CONTROL_PLANE_DB``, so a
  deployment that sets only ``CONTROL_PLANE_DB`` looked its secrets up in the wrong database.
"""

from __future__ import annotations

import json
import os
import posixpath
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest
from cryptography.fernet import Fernet

from examlops import secrets as sec
from examlops.platform_db import get_db, init_db
from examlops.secrets import inject, leases

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t-" + secrets.token_hex(8)  # generated at runtime: no credential-shaped literal


class _Store:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.leases: set[str] = set()
        self.counter = 0


def _handler(store: _Store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: dict | None = None, headers: dict | None = None) -> None:
            raw = json.dumps(body or {}).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _cleaned(self) -> bool:
            # Go's http.ServeMux: a non-canonical path is answered with a 301 to its clean form.
            path, _, query = self.path.partition("?")
            clean = posixpath.normpath(path)
            if path.endswith("/") and clean != "/":
                clean += "/"
            if clean != path:
                self._send(301, {}, {"Location": clean + (f"?{query}" if query else "")})
                return True
            return False

        def do_GET(self):  # noqa: N802
            if self._cleaned():
                return None
            if self.headers.get("X-Vault-Token") != TOKEN:
                return self._send(403, {"errors": ["permission denied"]})
            if self.path.startswith("/v1/secret/data/"):
                # Like the real server: the query string is not part of the key, and the path is
                # percent-decoded before lookup.
                key = unquote(self.path.split("?", 1)[0][len("/v1/secret/data/") :])
                if key not in store.kv:
                    return self._send(404, {"errors": []})
                return self._send(200, {"data": {"data": {"value": store.kv[key]}}})
            if "/creds/" in self.path:
                store.counter += 1
                lease = f"{self.path[len('/v1/') :]}/{store.counter}"
                store.leases.add(lease)
                return self._send(
                    200,
                    {
                        "lease_id": lease,
                        "lease_duration": 60,
                        "renewable": True,
                        "data": {"u": "x"},
                    },
                )
            return self._send(404, {"errors": []})

        def do_POST(self):  # noqa: N802
            if self._cleaned():
                return None
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.path[len("/v1/secret/data/") :]
            store.kv[key] = body["data"]["value"]
            return self._send(200, {"data": {"version": 1}})

        def do_PUT(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("lease_id") not in store.leases:
                return self._send(400, {"errors": ["invalid lease"]})
            if self.path == "/v1/sys/leases/revoke":
                store.leases.discard(body["lease_id"])
            return self._send(
                200, {"lease_id": body["lease_id"], "lease_duration": 60, "renewable": True}
            )

    return H


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    store = _Store()
    server = HTTPServer(("127.0.0.1", 0), _handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", TOKEN)
    monkeypatch.setenv("EXAMLOPS_SECRET_TENANTS", "acme,globex")
    for var in (
        "EXAMLOPS_SOPS_FILE",
        "EXAMLOPS_VAULT_STRICT",
        "EXAMLOPS_VAULT_MOUNT",
        "EXAMLOPS_MULTITENANCY",
        "EXAMLOPS_SECRETS_WRITE_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    init_db()
    yield store
    server.shutdown()


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT details FROM audit_events WHERE action=?", (action,)).fetchall()
    return [json.loads(r["details"] or "{}") for r in rows]


def test_the_fake_really_cleans_paths_like_go(vault):
    """Proves the premise: through this server a dot-segment path reaches another key."""
    import urllib.request

    vault.kv["globex/key"] = "globex-only"
    req = urllib.request.Request(
        f"{os.environ['EXAMLOPS_VAULT_ADDR']}/v1/secret/data/acme/../globex/key",
        headers={"X-Vault-Token": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback fake
        assert json.loads(resp.read())["data"]["data"]["value"] == "globex-only"


@pytest.mark.parametrize("path", ["acme/../globex/key", "/globex/key", "acme//../globex/key"])
def test_a_dot_segment_or_empty_segment_path_cannot_reach_another_tenant(vault, path):
    vault.kv["globex/key"] = "globex-only"
    with pytest.raises(sec.InvalidSecretPath):
        sec.get_secret(path, tenant="acme")
    assert _audit("secret_denied")[-1]["reason"] == "invalid_path"


def test_a_query_character_stays_inside_the_vault_path(vault):
    vault.kv["acme/x"] = "a-different-secret"
    with pytest.raises(sec.SecretNotFound):
        sec.get_secret("acme/x?version=1", tenant="acme")


def test_an_invalid_path_is_refused_for_writes_too(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    with pytest.raises(sec.InvalidSecretPath):
        sec.set_secret("acme/../globex/key", "x", tenant="acme")
    assert "globex/key" not in vault.kv


def test_a_tenant_may_not_overwrite_a_shared_secret_under_multitenancy(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    vault.kv["db-password"] = "shared-original"
    with pytest.raises(sec.SecretAccessDenied):
        sec.set_secret("db-password", "hijacked", tenant="acme")
    assert vault.kv["db-password"] == "shared-original"
    sec.set_secret("acme/db-password", "mine", tenant="acme")  # its own prefix is fine
    sec.set_secret("db-password", "rotated", tenant="admin")  # admin may rotate the shared one
    assert vault.kv["db-password"] == "rotated"


def test_single_tenant_mode_still_writes_shared_paths(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    sec.set_secret("db-password", "v1")
    assert vault.kv["db-password"] == "v1"


def test_lease_renew_and_revoke_are_tenant_scoped(vault):
    lease = leases.issue("globex/creds/ro", tenant="globex")["lease_id"]
    with pytest.raises(sec.SecretAccessDenied):
        leases.revoke(lease, tenant="acme")
    with pytest.raises(sec.SecretAccessDenied):
        leases.renew(lease, tenant="acme")
    assert lease in vault.leases
    assert _audit("secret_denied")[-1]["op"] == "renew"
    leases.revoke(lease, tenant="globex")
    assert lease not in vault.leases


def test_lease_issue_refuses_a_dot_segment_path(vault):
    with pytest.raises(leases.LeaseError, match="invalid engine path"):
        leases.issue("acme/./../globex/creds/ro", tenant="acme")


def test_disabled_injection_removes_references_instead_of_leaving_the_literal(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_INJECT", "0")
    env = {"CONTROL_PLANE_TOKEN": "secret://control-plane/token", "OTHER": "plain"}
    report = inject.inject_env("svc", env)
    assert "CONTROL_PLANE_TOKEN" not in env
    assert env["OTHER"] == "plain"
    assert report.removed == ["CONTROL_PLANE_TOKEN"]


def test_control_plane_resolves_from_control_plane_db_when_platform_db_is_unset(tmp_path):
    cp_db = tmp_path / "approvals.db"
    key = Fernet.generate_key().decode()
    seed = (
        "import os; from examlops import secrets as s; from examlops.platform_db import init_db; "
        "init_db(); s.set_secret('control-plane/token', 'cp-db-token-value', actor='op')"
    )
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PLATFORM_DB", "EXAMLOPS_DATA_DIR"}
        and not k.startswith(("EXAMLOPS_SECRETS", "EXAMLOPS_VAULT", "EXAMLOPS_SOPS"))
    }
    base["PYTHONPATH"] = os.pathsep.join([str(ROOT / "platform" / "cli" / "src"), str(ROOT)])
    base["EXAMLOPS_SECRETS_KEY"] = key
    # Anything that does NOT honour CONTROL_PLANE_DB lands in this empty data root instead of
    # the checkout's own platform.db.
    base["EXAMLOPS_DATA_DIR"] = str(tmp_path / "elsewhere")
    seeded = subprocess.run(
        [sys.executable, "-c", seed],
        env={**base, "PLATFORM_DB": str(cp_db)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert seeded.returncode == 0, seeded.stderr[-2000:]
    code = (
        "import sys; sys.path.insert(0, 'platform/services/control_plane'); "
        "import app; print('TOKEN=' + app.CONTROL_PLANE_TOKEN)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **base,
            "CONTROL_PLANE_DB": str(cp_db),
            "CONTROL_PLANE_TOKEN": "secret://control-plane/token",
        },
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "TOKEN=cp-db-token-value" in proc.stdout


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    audit_mod.reset_dropped_audit_events()


def test_a_lost_injection_audit_is_counted(tmp_path, monkeypatch):
    """Start-up still succeeds, and the missing ``secrets_injected`` record is counted."""
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    secret_file = tmp_path / "tok"
    secret_file.write_text("file-token\n")
    _break_the_audit_log(monkeypatch)
    try:
        env = {"SOME_TOKEN": f"secret+file://{secret_file}"}
        inject.inject_env("svc", env)
        assert env["SOME_TOKEN"] == "file-token"
        assert "secrets_injected" in dropped_audit_events()
    finally:
        reset_dropped_audit_events()


def test_a_lost_lease_audit_is_counted(vault, monkeypatch):
    """The revocation still happens at the manager, and its missing audit record is counted."""
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    lease = leases.issue("globex/creds/ro", tenant="globex")["lease_id"]
    _break_the_audit_log(monkeypatch)
    try:
        leases.revoke(lease, tenant="globex")
        assert lease not in vault.leases
        assert "secret_lease_revoked" in dropped_audit_events()
    finally:
        reset_dropped_audit_events()
