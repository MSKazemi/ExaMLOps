"""ADR 0011 clause 3 — dynamic short-lived credentials (leases) from OpenBao/Vault.

The fake speaks OpenBao's dynamic-engine and ``sys/leases`` wire format over real HTTP: a read of
``database/creds/<role>`` mints a fresh credential with a lease; ``sys/leases/renew`` and
``sys/leases/revoke`` act on it; a KV path answers with no lease.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import get_db, init_db
from examlops.secrets import SecretAccessDenied, leases

TOKEN = "t-" + secrets.token_hex(8)  # generated at runtime: no credential-shaped literal


class _Engine:
    def __init__(self) -> None:
        self.leases: dict[str, int] = {}
        self.counter = 0


def _handler(engine: _Engine):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: dict | None = None) -> None:
            raw = json.dumps(body or {}).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.headers.get("X-Vault-Token") != TOKEN:
                return self._send(403, {"errors": ["permission denied"]})
            if self.path.startswith("/v1/database/creds/"):
                engine.counter += 1
                lease = f"database/creds/ro/{engine.counter}"
                engine.leases[lease] = 3600
                return self._send(
                    200,
                    {
                        "lease_id": lease,
                        "lease_duration": 3600,
                        "renewable": True,
                        "data": {"username": f"v-ro-{engine.counter}", "password": "p"},
                    },
                )
            if self.path.startswith("/v1/secret/data/"):
                return self._send(200, {"lease_id": "", "data": {"data": {"value": "x"}}})
            return self._send(404, {"errors": []})

        def do_PUT(self):  # noqa: N802
            if self.headers.get("X-Vault-Token") != TOKEN:
                return self._send(403, {"errors": ["permission denied"]})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            lease = body.get("lease_id")
            if lease not in engine.leases:
                return self._send(400, {"errors": ["invalid lease"]})
            if self.path == "/v1/sys/leases/renew":
                engine.leases[lease] = min(int(body.get("increment") or 3600), 7200)
                return self._send(
                    200,
                    {"lease_id": lease, "lease_duration": engine.leases[lease], "renewable": True},
                )
            if self.path == "/v1/sys/leases/revoke":
                del engine.leases[lease]
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send(404, {"errors": []})

    return H


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    eng = _Engine()
    server = HTTPServer(("127.0.0.1", 0), _handler(eng))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", TOKEN)
    monkeypatch.delenv("EXAMLOPS_SECRET_TENANTS", raising=False)
    init_db()
    yield eng
    server.shutdown()


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT details FROM audit_events WHERE action=?", (action,)).fetchall()
    return [json.loads(r["details"] or "{}") for r in rows]


def test_issue_mints_a_fresh_credential_per_call_and_audits_without_it(engine):
    a = leases.issue("database/creds/ro", actor="t")
    b = leases.issue("database/creds/ro", actor="t")
    assert a["lease_id"] != b["lease_id"] and a["data"]["username"] != b["data"]["username"]
    assert a["lease_duration"] == 3600 and a["renewable"] is True
    rec = _audit("secret_lease_issued")[-1]
    assert rec["lease_id"] == b["lease_id"] and rec["fields"] == ["password", "username"]
    assert "v-ro-2" not in json.dumps(rec)


def test_a_static_kv_path_is_refused_as_not_short_lived(engine):
    with pytest.raises(leases.LeaseError, match="not a dynamic secrets engine"):
        leases.issue("secret/data/x", actor="t")


def test_renew_and_revoke(engine):
    lease = leases.issue("database/creds/ro", actor="t")["lease_id"]
    out = leases.renew(lease, increment=9999, actor="t")
    assert out["lease_duration"] == 7200  # the engine caps it
    leases.revoke(lease, actor="t")
    assert lease not in engine.leases
    assert _audit("secret_lease_revoked")[-1] == {"tenant": "default"}
    with pytest.raises(leases.LeaseError, match="HTTP 400"):
        leases.revoke(lease, actor="t")  # already gone


def test_bounds_and_failures_fail_closed(engine, monkeypatch):
    with pytest.raises(leases.LeaseError, match="TTL"):
        leases.renew("x", increment=0)
    with pytest.raises(leases.LeaseError, match="TTL"):
        leases.renew("x", increment=leases.MAX_TTL_SECONDS + 1)
    with pytest.raises(leases.LeaseError, match="invalid engine path"):
        leases.issue("../sys/raw")
    with pytest.raises(leases.LeaseError, match="empty"):
        leases.revoke(" ")
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", "wrong")
    with pytest.raises(leases.LeaseError, match="HTTP 403"):
        leases.issue("database/creds/ro")
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR")
    with pytest.raises(leases.LeaseError, match="EXAMLOPS_VAULT_ADDR"):
        leases.issue("database/creds/ro")


def test_tenant_prefix_applies_to_issue(engine, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRET_TENANTS", "acme")
    with pytest.raises(SecretAccessDenied):
        leases.issue("acme/creds/ro", tenant="globex")
    assert _audit("secret_denied")[-1]["op"] == "lease"


def test_cli_issue_redacts_by_default_and_revoke_works(engine):
    runner = CliRunner()
    res = runner.invoke(app, ["--json", "secrets", "lease", "issue", "database/creds/ro"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["data"] is None and payload["lease_id"].startswith("database/creds/ro/")
    res = runner.invoke(app, ["secrets", "lease", "issue", "database/creds/ro"])
    assert res.exit_code == 0 and "v-ro-2" not in res.output and "••••" in res.output
    res = runner.invoke(
        app, ["--json", "secrets", "lease", "issue", "database/creds/ro", "--reveal"]
    )
    assert json.loads(res.stdout)["data"]["username"] == "v-ro-3"
    res = runner.invoke(app, ["--json", "secrets", "lease", "renew", payload["lease_id"]])
    assert res.exit_code == 0 and json.loads(res.stdout)["lease_duration"] == 3600
    res = runner.invoke(app, ["--yes", "secrets", "lease", "revoke", payload["lease_id"]])
    assert res.exit_code == 0 and payload["lease_id"] not in engine.leases


def test_cli_issue_failure_exits_1(engine, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR")
    res = CliRunner().invoke(app, ["secrets", "lease", "issue", "database/creds/ro"])
    assert res.exit_code == 1 and "EXAMLOPS_VAULT_ADDR" in res.output
