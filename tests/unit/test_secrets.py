# tests/unit/test_secrets.py
"""D7 — Secrets management & rotation (ADR 0011, spec D7).

GWT-2 fail-fast · GWT-3 rotation+audit · GWT-4 scanner · GWT-5 tenant scoping.
(GWT-1/6 remediation-order are runbook/CI concerns, exercised via the scanner + docs.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import secrets as sec  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402


def _audit_actions() -> list[str]:
    with get_db() as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR", raising=False)
    monkeypatch.delenv("EXAMLOPS_SECRET_TENANTS", raising=False)
    init_db()


def test_set_get_roundtrip_encrypted():
    sec.set_secret("control-plane/token", "s3cr3t", actor="me")
    assert sec.get_secret("control-plane/token", actor="me") == "s3cr3t"
    # ciphertext in the store is not the plaintext
    from examlops.platform_db import get_secret_ciphertext

    ct = get_secret_ciphertext("control-plane/token", "default")
    assert ct is not None and "s3cr3t" not in ct


def test_gwt2_missing_fails_fast():
    with pytest.raises(sec.SecretNotFound):
        sec.get_secret("does/not/exist", actor="me")


def test_env_fallback():
    import os

    os.environ["MY_SVC_TOKEN"] = "from-env"
    try:
        assert sec.get_secret("my/svc/token", actor="me") == "from-env"
    finally:
        del os.environ["MY_SVC_TOKEN"]


def test_gwt3_rotation_changes_value_and_audits():
    sec.set_secret("cp/token", "old", actor="me")
    old = sec.get_secret("cp/token", actor="me")
    sec.rotate_secret("cp/token", actor="me")
    new = sec.get_secret("cp/token", actor="me")
    assert new != old
    assert "secret_rotate" in _audit_actions()


def test_gwt5_tenant_scoping_denied(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRET_TENANTS", "acme,globex")
    sec.set_secret("acme/api-key", "k", tenant="acme", actor="me")
    # the owning tenant reads it fine
    assert sec.get_secret("acme/api-key", tenant="acme", actor="me") == "k"
    # another tenant may not read acme's path — denied and audited
    with pytest.raises(sec.SecretAccessDenied):
        sec.get_secret("acme/api-key", tenant="globex", actor="mallory")
    assert "secret_denied" in _audit_actions()


def test_list_never_returns_values():
    sec.set_secret("a/b", "v", actor="me")
    rows = sec.list_secrets()
    assert rows and "value" not in rows[0] and "ciphertext" not in rows[0]


# --- GWT-4: scanner ----------------------------------------------------------


def test_gwt4_scan_detects_aws_key():
    # Assemble the AWS-shaped token from fragments so this test file itself carries no
    # literal that trips secret scanners — the runtime string still exercises the detector.
    fake_aws = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    findings = sec.scan_text(f'aws_key = "{fake_aws}"')
    assert any(f["rule"] == "aws-access-key" for f in findings)


def test_scan_detects_private_key_and_api_key():
    header = "-----BEGIN " + "PRIVATE KEY-----"
    key_name = "api" + "_key"  # keep the "<name> = \"...\"" literal out of the source
    fake_api = "abcdef0123456789abcd"
    text = f'{header}\n{key_name} = "{fake_api}"'
    rules = {f["rule"] for f in sec.scan_text(text)}
    assert "private-key" in rules
    assert "generic-api-key" in rules


def test_scan_clean_text_no_findings():
    assert sec.scan_text("just a normal line of code x = 1") == []


def test_no_encryption_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SECRETS_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    with pytest.raises(sec.SecretNotFound, match="encryption key"):
        sec.set_secret("x/y", "v", actor="me")


# --- the vault backend: which store actually served the value ----------------
# The vault is the FIRST of three backends and the only one outside this process's trust
# domain, yet a configured-but-unreachable vault used to fall through to the local store
# (or to a plain environment variable) with nothing logged and nothing in the audit row to
# say so. These tests pin the two properties that makes safe: the fallback still happens,
# and it is never silent.


def _audit_details(action: str = "secret_access") -> list[dict]:
    import json

    with get_db() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_events WHERE action = ?", (action,)
        ).fetchall()
    return [json.loads(r["details"]) if r["details"] else {} for r in rows]


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _vault_returning(value: str):
    import json

    body = json.dumps({"data": {"data": {"value": value}}}).encode()
    return lambda *a, **k: _FakeResp(body)


def _vault_raising(exc: Exception):
    def _boom(*a, **k):
        raise exc

    return _boom


def test_vault_serves_the_value_and_the_audit_names_it(monkeypatch):
    import urllib.request

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://vault.invalid:8200")
    monkeypatch.setattr(urllib.request, "urlopen", _vault_returning("from-vault"))
    sec.set_secret("cp/token", "from-local", actor="me")

    assert sec.get_secret("cp/token", actor="me") == "from-vault"
    assert _audit_details()[-1]["backend"] == "vault"


def test_unreachable_vault_falls_back_but_never_silently(monkeypatch, caplog):
    import urllib.request

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://vault.invalid:8200")
    monkeypatch.setattr(urllib.request, "urlopen", _vault_raising(OSError("connection refused")))
    sec.set_secret("cp/token", "from-local", actor="me")

    with caplog.at_level("WARNING", logger="examlops.secrets"):
        assert sec.get_secret("cp/token", actor="me") == "from-local"

    assert any("connection refused" in r.getMessage() for r in caplog.records)
    detail = _audit_details()[-1]
    assert detail["backend"] == "local"
    assert "connection refused" in detail["vault_error"]


def test_unreachable_vault_downgrading_to_an_env_var_is_recorded_as_env(monkeypatch):
    import urllib.request

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://vault.invalid:8200")
    monkeypatch.setenv("MY_SVC_TOKEN", "from-env")
    monkeypatch.setattr(urllib.request, "urlopen", _vault_raising(OSError("timed out")))

    assert sec.get_secret("my/svc/token", actor="me") == "from-env"
    detail = _audit_details()[-1]
    assert detail["backend"] == "env"
    assert detail.get("vault_error")


def test_vault_saying_not_found_is_not_a_degradation(monkeypatch, caplog):
    """A 404 means the vault answered: this secret is not there. Falling through is correct."""
    import urllib.error
    import urllib.request

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://vault.invalid:8200")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _vault_raising(urllib.error.HTTPError("u", 404, "Not Found", {}, None)),  # type: ignore[arg-type]
    )
    sec.set_secret("cp/token", "from-local", actor="me")

    with caplog.at_level("WARNING", logger="examlops.secrets"):
        assert sec.get_secret("cp/token", actor="me") == "from-local"

    assert not caplog.records
    detail = _audit_details()[-1]
    assert detail["backend"] == "local"
    assert "vault_error" not in detail


def test_strict_mode_refuses_to_downgrade(monkeypatch):
    """EXAMLOPS_VAULT_STRICT: a vault outage fails the read instead of serving another store."""
    import urllib.request

    monkeypatch.setenv("EXAMLOPS_VAULT_ADDR", "http://vault.invalid:8200")
    monkeypatch.setenv("EXAMLOPS_VAULT_STRICT", "1")
    monkeypatch.setattr(urllib.request, "urlopen", _vault_raising(OSError("connection refused")))
    sec.set_secret("cp/token", "from-local", actor="me")

    with pytest.raises(sec.SecretNotFound) as exc:
        sec.get_secret("cp/token", actor="me")
    assert "connection refused" in str(exc.value)
    assert _audit_details()[-1]["backend"] == "none"


def test_no_vault_configured_is_not_reported_as_an_error(monkeypatch, caplog):
    sec.set_secret("cp/token", "from-local", actor="me")
    with caplog.at_level("WARNING", logger="examlops.secrets"):
        assert sec.get_secret("cp/token", actor="me") == "from-local"
    assert not caplog.records
    detail = _audit_details()[-1]
    assert detail["backend"] == "local" and "vault_error" not in detail
