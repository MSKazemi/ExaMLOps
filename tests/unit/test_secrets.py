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
