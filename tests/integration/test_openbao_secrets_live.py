"""ADR 0011 against a REAL OpenBao: write-to-manager rotation, reads, health, and startup injection.

The unit suite drives the client against a faithful KV v2 fake. This one runs it against the
OpenBao image the Compose ``secrets`` profile pins, in dev mode (KV v2 at ``secret/``):

    docker run -d --rm --name openbao-live -p 127.0.0.1:18299:8200 \\
        -e BAO_DEV_ROOT_TOKEN_ID=<token> openbao/openbao:2.6.2 server -dev \\
        -dev-listen-address=0.0.0.0:8200
    EXAMLOPS_TEST_OPENBAO_ADDR=http://127.0.0.1:18299 EXAMLOPS_TEST_OPENBAO_TOKEN=<token> \\
        .venv/bin/pytest tests/integration/test_openbao_secrets_live.py -v

Opt-in: skipped unless both variables are set.
"""

from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet

ADDR = os.getenv("EXAMLOPS_TEST_OPENBAO_ADDR", "")
TOKEN = os.getenv("EXAMLOPS_TEST_OPENBAO_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (ADDR and TOKEN), reason="set EXAMLOPS_TEST_OPENBAO_ADDR/_TOKEN to run against OpenBao"
)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    from examlops.platform_db import init_db

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", ADDR)
    monkeypatch.setenv("EXAMLOPS_VAULT_TOKEN", TOKEN)
    monkeypatch.setenv("EXAMLOPS_VAULT_STRICT", "1")
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    monkeypatch.delenv("EXAMLOPS_SOPS_FILE", raising=False)
    monkeypatch.delenv("EXAMLOPS_VAULT_MOUNT", raising=False)
    init_db()
    return f"live-{uuid.uuid4().hex[:8]}/token"


def test_rotation_writes_new_kv_versions_in_openbao(live):
    from examlops import secrets as sec

    v1 = sec.set_secret(live, "first", actor="live")
    v2 = sec.rotate_secret(live, actor="live")
    assert v2 == v1 + 1
    res = sec.resolve_secret(live, actor="live")
    assert res["backend"] == "vault" and res["value"] != "first" and len(res["value"]) >= 32
    assert sec.list_secrets() == []  # nothing mirrored into the local store


def test_health_reports_an_unsealed_initialised_server(live):
    from examlops import secrets as sec

    st = sec.backends_status()["vault"]
    assert st["reachable"] and st["initialized"] and not st["sealed"]


def test_startup_injection_reads_from_openbao(live):
    from examlops import secrets as sec
    from examlops.secrets.inject import inject_env

    sec.set_secret(live, "injected-from-openbao", actor="live")
    env = {"SOME_TOKEN": f"secret://{live}"}
    report = inject_env("live-test", env)
    assert env["SOME_TOKEN"] == "injected-from-openbao"
    assert report.injected[0].backend == "vault"


def test_a_missing_key_is_not_an_outage(live):
    from examlops import secrets as sec

    with pytest.raises(sec.SecretNotFound, match="not found"):
        sec.resolve_secret(f"{live}-absent", actor="live")
