"""ADR 0011 clause 1 — the SOPS + age fallback tier of the secrets client.

The fake ``sops`` (``tests/unit/_fake_sops.py``) runs as a real subprocess, so these cover the
backend's actual argv, exit-code and stderr handling. ``test_real_sops_and_age_roundtrip`` runs
the genuine binaries when they are installed (or named by ``EXAMLOPS_TEST_SOPS_BIN`` /
``EXAMLOPS_TEST_AGE_KEYGEN``) and skips otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from examlops import secrets as sec
from examlops.platform_db import get_db, init_db
from examlops.secrets import sops

FAKE = Path(__file__).with_name("_fake_sops.py")


def _audit_rows(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT action, details FROM audit_events WHERE action=?", (action,)
        ).fetchall()
    return [{"action": r["action"], "details": json.loads(r["details"] or "{}")} for r in rows]


@pytest.fixture()
def fake_sops(tmp_path, monkeypatch):
    wrapper = tmp_path / "sops"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    doc = tmp_path / "secrets.enc.json"
    doc.write_text(json.dumps({"control-plane": {"token": "from-sops"}, "acme": {"k": "a-val"}}))
    log = tmp_path / "argv.log"
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR", raising=False)
    monkeypatch.delenv("EXAMLOPS_VAULT_STRICT", raising=False)
    monkeypatch.delenv("EXAMLOPS_SECRETS_WRITE_BACKEND", raising=False)
    monkeypatch.setenv("EXAMLOPS_SOPS_BIN", str(wrapper))
    monkeypatch.setenv("EXAMLOPS_SOPS_FILE", str(doc))
    monkeypatch.setenv("FAKE_SOPS_IDENTITY", "ok")
    monkeypatch.setenv("FAKE_SOPS_ARGV_LOG", str(log))
    init_db()
    return {"doc": doc, "log": log}


def test_index_for_maps_path_segments_and_rejects_traversal():
    assert sops.index_for("control-plane/token") == '["control-plane"]["token"]'
    assert sops.index_for('we"ird') == '["we\\"ird"]'
    for bad in ("", "a//b", "../x", "a/./b"):
        with pytest.raises(ValueError):
            sops.index_for(bad)


def test_resolve_prefers_sops_over_local_and_audits_backend(fake_sops):
    sec.set_secret("control-plane/token", "from-local", actor="t")  # local write (default)
    res = sec.resolve_secret("control-plane/token", actor="t")
    assert res["value"] == "from-sops"
    assert res["backend"] == "sops"
    access = _audit_rows("secret_access")[-1]["details"]
    assert access["backend"] == "sops" and "sops_error" not in access


def test_key_missing_from_the_document_falls_through_quietly(fake_sops):
    sec.set_secret("only/local", "L", actor="t")
    res = sec.resolve_secret("only/local", actor="t")
    assert (res["value"], res["backend"]) == ("L", "local")
    assert "sops_error" not in _audit_rows("secret_access")[-1]["details"]


def test_no_identity_is_an_outage_reported_not_a_miss(fake_sops, monkeypatch, caplog):
    monkeypatch.delenv("FAKE_SOPS_IDENTITY")
    sec.set_secret("control-plane/token", "from-local", actor="t")
    res = sec.resolve_secret("control-plane/token", actor="t")
    assert res["backend"] == "local"
    details = _audit_rows("secret_access")[-1]["details"]
    assert "exited 128" in details["sops_error"]
    assert "SOPS backend failed" in caplog.text


def test_strict_refuses_to_fall_back_on_a_sops_outage(fake_sops, monkeypatch):
    monkeypatch.delenv("FAKE_SOPS_IDENTITY")
    monkeypatch.setenv("EXAMLOPS_VAULT_STRICT", "1")
    sec.set_secret("control-plane/token", "from-local", actor="t")
    with pytest.raises(sec.SecretNotFound, match="SOPS backend failed"):
        sec.resolve_secret("control-plane/token", actor="t")


def test_missing_binary_and_missing_file_are_outages(fake_sops, monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_SOPS_BIN", str(tmp_path / "nope"))
    assert sops.get("control-plane/token")[1].startswith("sops is not installed")
    monkeypatch.setenv("EXAMLOPS_SOPS_BIN", str(tmp_path / "sops"))
    monkeypatch.setenv("EXAMLOPS_SOPS_FILE", str(tmp_path / "absent.json"))
    assert "does not exist" in sops.get("control-plane/token")[1]


def test_unconfigured_tier_is_silent(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SOPS_FILE", raising=False)
    assert sops.get("x/y") == (None, None)
    assert sops.status()["configured"] is False


def test_timeout_is_bounded_and_reported(fake_sops, monkeypatch, tmp_path):
    slow = tmp_path / "slow-sops"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setenv("EXAMLOPS_SOPS_BIN", str(slow))
    monkeypatch.setenv("EXAMLOPS_SOPS_TIMEOUT", "0.3")
    value, err = sops.get("control-plane/token")
    assert value is None and "timed out" in err


def test_write_backend_sops_sets_value_via_stdin_never_argv(fake_sops, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "sops")
    version = sec.set_secret("control-plane/token", "n3w-s3cr3t", actor="t")
    assert version == 0  # SOPS is unversioned
    assert json.loads(fake_sops["doc"].read_text())["control-plane"]["token"] == "n3w-s3cr3t"
    assert "n3w-s3cr3t" not in fake_sops["log"].read_text()
    assert sec.resolve_secret("control-plane/token", actor="t")["value"] == "n3w-s3cr3t"
    assert _audit_rows("secret_set")[-1]["details"]["backend"] == "sops"
    # nothing was written to the local store
    from examlops.data.secrets import list_secret_paths

    assert list_secret_paths() == []


def test_rotate_in_sops_changes_the_value(fake_sops, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "sops")
    sec.rotate_secret("control-plane/token", actor="t")
    rotated = json.loads(fake_sops["doc"].read_text())["control-plane"]["token"]
    assert rotated != "from-sops" and len(rotated) >= 32
    assert _audit_rows("secret_rotate")[-1]["details"]["backend"] == "sops"


def test_sops_write_failure_fails_closed_and_is_audited(fake_sops, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "sops")
    monkeypatch.delenv("FAKE_SOPS_IDENTITY")
    with pytest.raises(sec.SecretBackendError, match="exited 128"):
        sec.set_secret("control-plane/token", "v", actor="t")
    assert _audit_rows("secret_set_failed")
    from examlops.data.secrets import list_secret_paths

    assert list_secret_paths() == []  # no silent local write


def test_shared_store_write_enforces_the_tenant_prefix(fake_sops, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "sops")
    monkeypatch.setenv("EXAMLOPS_SECRET_TENANTS", "acme,globex")
    with pytest.raises(sec.SecretAccessDenied):
        sec.set_secret("acme/k", "overwrite", tenant="globex", actor="t")
    assert json.loads(fake_sops["doc"].read_text())["acme"]["k"] == "a-val"
    assert _audit_rows("secret_denied")[-1]["details"]["op"] == "write"


def test_unknown_write_backend_is_refused(fake_sops, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "s3")
    with pytest.raises(sec.SecretBackendError, match="not one of"):
        sec.set_secret("a/b", "v", actor="t")


def test_status_reports_version_and_file(fake_sops):
    st = sops.status()
    assert st["configured"] and st["file_exists"] and st["error"] is None
    assert st["version"].startswith("sops 3.13.3")


def _real_tool(env_name: str, default: str) -> str | None:
    explicit = os.getenv(env_name)
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which(default)


def test_real_sops_and_age_roundtrip(tmp_path, monkeypatch):
    sops_bin = _real_tool("EXAMLOPS_TEST_SOPS_BIN", "sops")
    keygen = _real_tool("EXAMLOPS_TEST_AGE_KEYGEN", "age-keygen")
    if not sops_bin or not keygen:
        pytest.skip("real sops + age-keygen not installed")
    key = tmp_path / "age.key"
    subprocess.run([keygen, "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        ln.split(":", 1)[1].strip() for ln in key.read_text().splitlines() if "public key" in ln
    )
    plain = tmp_path / "plain.yaml"
    plain.write_text("control-plane:\n  token: real-sops-value\n")
    enc = tmp_path / "secrets.enc.yaml"
    out = subprocess.run(
        [sops_bin, "encrypt", "--age", recipient, str(plain)],
        check=True,
        capture_output=True,
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(key)},
    )
    enc.write_bytes(out.stdout)
    assert b"real-sops-value" not in enc.read_bytes()  # the committed file holds no plaintext
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    monkeypatch.setenv("EXAMLOPS_SOPS_BIN", sops_bin)
    monkeypatch.setenv("EXAMLOPS_SOPS_FILE", str(enc))
    monkeypatch.delenv("EXAMLOPS_VAULT_ADDR", raising=False)
    init_db()
    assert sops.get("control-plane/token") == ("real-sops-value", None)
    assert sops.get("control-plane/missing") == (None, None)
    sops.put("control-plane/token", "rotated\nvalue")
    assert sops.get("control-plane/token") == ("rotated\nvalue", None)
    assert b"rotated" not in enc.read_bytes()
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(tmp_path / "wrong.key"))
    value, err = sops.get("control-plane/token")
    assert value is None and err and "exited" in err
