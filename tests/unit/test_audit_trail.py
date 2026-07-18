"""D4 — immutable, tamper-evident audit trail (ADR 0028).

GWT acceptance criteria from ``design/vision/specs/D4-immutable-audit-trail.md`` §5.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_events_are_hash_chained():
    """R1: each event stores prev_hash + hash forming a chain."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "alice", "a1", "JPCP")
    platform_db.write_audit_event("cli", "bob", "a2", "JPCP")
    with platform_db.get_db() as conn:
        rows = conn.execute("SELECT prev_hash, hash FROM audit_events ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] == "GENESIS"
    assert rows[0]["hash"]
    assert rows[1]["prev_hash"] == rows[0]["hash"]  # chained


def test_verify_clean_chain():
    from examlops import platform_db

    for i in range(5):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    result = platform_db.verify_audit_chain()
    assert result["ok"] is True
    assert result["count"] == 5


def test_gwt2_tamper_breaks_chain():
    """R2: an edit causes verification to fail and identifies the first broken link."""
    from examlops import platform_db

    for i in range(5):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")

    # Tamper by writing directly to the raw file DB, bypassing triggers is not possible;
    # temporarily drop the trigger to simulate an attacker with DB access.
    path = platform_db._db_path()
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    raw.execute("UPDATE audit_events SET action='HACKED' WHERE id=3")
    raw.commit()
    raw.close()

    result = platform_db.verify_audit_chain()
    assert result["ok"] is False
    assert result["broken_at_id"] == 3


def test_gwt3_append_only_delete_blocked():
    """R3: DELETE on audit_events is blocked at the DB level."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    with platform_db.get_db() as conn:  # noqa: SIM117
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_events")


def test_gwt3_append_only_update_blocked():
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    with platform_db.get_db() as conn:  # noqa: SIM117
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_events SET action='x'")


def test_gwt5_checkpoint_signed_over_head():
    """R5: the chain head can be signed, producing a provable detached checkpoint."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    head = platform_db.audit_chain_head()
    cp = platform_db.sign_audit_checkpoint("sig-abc", key_id="test")
    assert cp["head_id"] == head["id"]
    assert cp["head_hash"] == head["hash"]
    assert platform_db.list_audit_checkpoints()[0]["signature"] == "sig-abc"


def test_export_is_read_only():
    """R4: export reads events without deleting (append-only)."""
    from examlops import platform_db

    for i in range(3):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    events = platform_db.export_audit_events()
    assert len(events) == 3
    # Events still present after export.
    assert len(platform_db.export_audit_events()) == 3


def test_gwt7_events_carry_actor_tenant_resource():
    """R7: events carry actor, tenant, and resource."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "alice", "promotion", "JPCP", tenant="acme")
    with platform_db.get_db() as conn:
        row = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor"] == "alice"
    assert row["tenant"] == "acme"
    assert row["target"] == "JPCP"


def test_cli_verify_and_checkpoint(monkeypatch):
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    # A real signing key must be configured to produce a non-forgeable checkpoint (item 0.7).
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    for i in range(3):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    runner = CliRunner()
    r1 = runner.invoke(app, ["audit", "verify"])
    assert r1.exit_code == 0, r1.output
    assert "verified" in r1.output.lower()
    r2 = runner.invoke(app, ["audit", "checkpoint"])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(app, ["audit", "checkpoints"])
    assert r3.exit_code == 0, r3.output


def test_cli_checkpoint_fails_closed_without_signing_key(monkeypatch):
    """0.7: with no signing key, checkpoint refuses (exit 1) instead of signing forgeably."""
    from typer.testing import CliRunner

    import examlops.supplychain as sc
    from examlops import platform_db
    from examlops.cli.main import app

    # Ensure neither the env key nor a D7 secret is available.
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)

    def _no_key() -> bytes:
        raise sc.SigningKeyMissing("no signing key")

    monkeypatch.setattr(sc, "_signing_key", _no_key)
    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    runner = CliRunner()
    r = runner.invoke(app, ["audit", "checkpoint"])
    assert r.exit_code == 1, r.output
    assert "refusing" in r.output.lower() or "cannot sign" in r.output.lower()
    # No forgeable checkpoint was recorded.
    assert platform_db.list_audit_checkpoints() == []


def test_cli_bare_audit_still_works():
    """Converting to a group must not break `exa audit --last`."""
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    runner = CliRunner()
    result = runner.invoke(app, ["audit", "--last", "7d"])
    assert result.exit_code == 0, result.output
