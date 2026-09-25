"""ADR 0011 clause 3 — rotation lands in the manager of record (OpenBao/Vault KV v2).

``EXAMLOPS_SECRETS_WRITE_BACKEND=vault`` sends ``set``/``rotate`` to the vault as a new KV v2
version. The fake below is a real HTTP server speaking the KV v2 + ``sys/health`` wire format
(token check, versions, 404 on a missing key), so the client's actual ``urllib`` path runs.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography.fernet import Fernet

from examlops import secrets as sec
from examlops.platform_db import get_db, init_db

TOKEN = "t-" + secrets.token_hex(8)  # generated: no credential-shaped literal in the tree


class _KV:
    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}
        self.sealed = False
        self.requests: list[tuple[str, str, dict]] = []


def _handler(kv: _KV):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: dict | None = None) -> None:
            raw = json.dumps(body or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _path(self) -> str | None:
            prefix = "/v1/kv/data/" if self.path.startswith("/v1/kv/") else "/v1/secret/data/"
            return self.path[len(prefix) :] if self.path.startswith(prefix) else None

        def do_GET(self):  # noqa: N802
            kv.requests.append(("GET", self.path, dict(self.headers)))
            if self.path.startswith("/v1/sys/health"):
                return self._send(200, {"initialized": True, "sealed": kv.sealed})
            if self.headers.get("X-Vault-Token") != TOKEN:
                return self._send(403, {"errors": ["permission denied"]})
            key = self._path()
            if key not in kv.data:
                return self._send(404, {"errors": []})
            versions = kv.data[key]
            return self._send(
                200,
                {"data": {"data": {"value": versions[-1]}, "metadata": {"version": len(versions)}}},
            )

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            kv.requests.append(("POST", self.path, dict(self.headers)))
            if self.headers.get("X-Vault-Token") != TOKEN:
                return self._send(403, {"errors": ["permission denied"]})
            key = self._path()
            kv.data.setdefault(key, []).append(body["data"]["value"])
            return self._send(200, {"data": {"version": len(kv.data[key])}})

    return H


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    kv = _KV()
    server = HTTPServer(("127.0.0.1", 0), _handler(kv))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", TOKEN)
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    for var in ("EXAMLOPS_SOPS_FILE", "EXAMLOPS_VAULT_STRICT", "EXAMLOPS_VAULT_MOUNT"):
        monkeypatch.delenv(var, raising=False)
    init_db()
    yield kv
    server.shutdown()


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT details FROM audit_events WHERE action=?", (action,)).fetchall()
    return [json.loads(r["details"] or "{}") for r in rows]


def test_set_writes_a_new_kv_version_and_reads_back_from_vault(vault):
    assert sec.set_secret("control-plane/token", "v1", actor="t") == 1
    assert sec.set_secret("control-plane/token", "v2", actor="t") == 2
    res = sec.resolve_secret("control-plane/token", actor="t")
    assert (res["value"], res["backend"]) == ("v2", "vault")
    assert _audit("secret_set")[-1] == {"tenant": "default", "backend": "vault", "version": 2}
    from examlops.data.secrets import list_secret_paths

    assert list_secret_paths() == []  # the local store holds no copy


def test_rotate_rotates_in_the_vault(vault):
    sec.set_secret("cp/token", "old", actor="t")
    version = sec.rotate_secret("cp/token", actor="t")
    assert version == 2
    assert vault.data["cp/token"][-1] != "old"
    assert _audit("secret_rotate")[-1]["backend"] == "vault"


def test_denied_token_fails_the_write_closed(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", "wrong")
    with pytest.raises(sec.SecretBackendError, match="HTTP 403"):
        sec.set_secret("cp/token", "v", actor="t")
    assert "error" in _audit("secret_set_failed")[-1]
    from examlops.data.secrets import list_secret_paths

    assert list_secret_paths() == []


def test_unreachable_vault_fails_the_write_closed(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://127.0.0.1:1")
    monkeypatch.setenv("EXAMLOPS_VAULT_TIMEOUT", "0.5")
    with pytest.raises(sec.SecretBackendError, match="unreachable"):
        sec.rotate_secret("cp/token", actor="t")
    assert not _audit("secret_rotate")  # a failed rotation is not recorded as a rotation


def test_vault_write_without_addr_is_refused(vault, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR")
    with pytest.raises(sec.SecretBackendError, match="EXAMLOPS_VAULT_ADDR"):
        sec.set_secret("cp/token", "v", actor="t")


def test_mount_and_namespace_are_honoured(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VAULT_MOUNT", "kv")
    monkeypatch.setenv("EXAMLOPS_VAULT_NAMESPACE", "team-a")
    sec.set_secret("x/y", "v", actor="t")
    method, path, headers = vault.requests[-1]
    assert (method, path) == ("POST", "/v1/kv/data/x/y")
    assert headers.get("X-Vault-Namespace") == "team-a"
    assert sec.get_secret("x/y", actor="t") == "v"


def test_backends_status_reports_health_without_secrets(vault):
    st = sec.backends_status()
    assert st["write_backend"] == "vault"
    assert st["vault"]["reachable"] is True and st["vault"]["sealed"] is False
    assert st["vault"]["token"] is True
    assert TOKEN not in json.dumps(st)
    # the health probe carries no token
    health = [r for r in vault.requests if r[1].startswith("/v1/sys/health")][-1]
    assert "X-Vault-Token" not in health[2]


def test_backends_status_reports_an_unreachable_vault(vault, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://127.0.0.1:1")
    monkeypatch.setenv("EXAMLOPS_VAULT_TIMEOUT", "0.5")
    st = sec.backends_status()
    assert st["vault"]["reachable"] is False and st["vault"]["error"]


def test_local_default_is_unchanged(vault, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SECRETS_WRITE_BACKEND")
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR")
    sec.set_secret("a/b", "local-v", actor="t")
    assert sec.resolve_secret("a/b", actor="t")["backend"] == "local"
    assert _audit("secret_set")[-1]["backend"] == "local"
