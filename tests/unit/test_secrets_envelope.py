"""Envelope encryption: per-secret key_id + online KEK rotation/rewrap (item 2.3).

Proves keys can rotate without downtime: new secrets are wrapped under the active KEK (its id
stored per row), old ciphertext keeps decrypting under its own key, `rewrap_secrets` migrates
everything to the active key, and a decommissioned key can then be dropped from the keyring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from cryptography.fernet import Fernet  # noqa: E402

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_SECRETS_KEYS",
        "EXAMLOPS_SECRETS_ACTIVE_KEY",
        "EXAMLOPS_SECRETS_KEY",
        "DASHBOARD_SECRET_KEY",
        "EXAMLOPS_VAULT_ADDR",
    ):
        monkeypatch.delenv(var, raising=False)
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb, monkeypatch


def test_new_secret_is_tagged_with_active_key_id(db):
    pdb, mp = db
    from examlops import secrets

    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A},kB:{KEY_B}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kB")
    secrets.set_secret("db/password", "hunter2", tenant="default", actor="admin")

    rec = pdb.get_secret_record("db/password", "default")
    assert rec["key_id"] == "kB"
    assert secrets.get_secret("db/password") == "hunter2"


def test_old_ciphertext_still_decrypts_after_adding_a_new_active_key(db):
    pdb, mp = db
    from examlops import secrets

    # Write under key A.
    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kA")
    secrets.set_secret("api/token", "old-secret")

    # Rotate: add key B as active, keep A in the ring for decryption.
    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A},kB:{KEY_B}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kB")
    assert secrets.get_secret("api/token") == "old-secret"  # still readable under A


def test_rewrap_migrates_all_secrets_to_active_key(db):
    pdb, mp = db
    from examlops import secrets

    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kA")
    secrets.set_secret("s1", "v1")
    secrets.set_secret("s2", "v2")

    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A},kB:{KEY_B}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kB")

    summary = secrets.rewrap_secrets(actor="admin")
    assert summary["rewrapped"] == 2 and summary["failed"] == 0
    assert pdb.get_secret_record("s1", "default")["key_id"] == "kB"
    assert pdb.get_secret_record("s2", "default")["key_id"] == "kB"
    # Values intact after rewrap.
    assert secrets.get_secret("s1") == "v1"
    assert secrets.get_secret("s2") == "v2"

    # Now key A can be decommissioned — secrets still readable under B alone.
    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kB:{KEY_B}")
    assert secrets.get_secret("s1") == "v1"


def test_rewrap_is_idempotent(db):
    pdb, mp = db
    from examlops import secrets

    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kB:{KEY_B}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kB")
    secrets.set_secret("s1", "v1")
    summary = secrets.rewrap_secrets()
    assert summary["rewrapped"] == 0 and summary["skipped"] == 1  # already on active key


def test_rewrap_dry_run_changes_nothing(db):
    pdb, mp = db
    from examlops import secrets

    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kA")
    secrets.set_secret("s1", "v1")
    mp.setenv("EXAMLOPS_SECRETS_KEYS", f"kA:{KEY_A},kB:{KEY_B}")
    mp.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "kB")

    summary = secrets.rewrap_secrets(dry_run=True)
    assert summary["rewrapped"] == 1 and summary["dry_run"] is True
    assert pdb.get_secret_record("s1", "default")["key_id"] == "kA"  # unchanged


def test_legacy_dashboard_key_still_decrypts_but_is_not_primary(db):
    pdb, mp = db
    from examlops import secrets

    # Legacy: only DASHBOARD_SECRET_KEY set → writes tagged 'legacy-dashboard', still work.
    mp.setenv("DASHBOARD_SECRET_KEY", KEY_A)
    secrets.set_secret("legacy/s", "v")
    rec = pdb.get_secret_record("legacy/s", "default")
    assert rec["key_id"] == "legacy-dashboard"
    assert secrets.get_secret("legacy/s") == "v"
