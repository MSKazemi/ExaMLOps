"""Audit retention that keeps the hash chain verifiable (ADR 0028 decision 4).

The claim under test: rows older than a configured minimum period can be pruned, ``exa audit
verify`` still passes over what remains, and it still FAILS if a retained row is tampered with, if
the prune record is forged, or if the prune record is removed. Real code end to end: the real
append-only triggers, the real chain, the real WORM anchor file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.resilience.db import connect

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_PATH", str(tmp_path / "worm.jsonl"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    from examlops.platform_db import init_db

    init_db()
    return tmp_path


def _add(conn, ts: str, action: str) -> None:
    """Append one correctly chained event with an explicit timestamp (so we can make old rows)."""
    from examlops.data.audit import _CORRELATION_COLS
    from examlops.platform_db import _audit_canonical, _audit_hash

    head = conn.execute(
        "SELECT hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = head[0] if head else "GENESIS"
    corr = {c: None for c in _CORRELATION_COLS}
    canonical = _audit_canonical("cli", "alice", action, "JPCP", None, "default", ts, corr)
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details, tenant, prev_hash, "
        "hash, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        ("cli", "alice", action, "JPCP", None, "default", prev, _audit_hash(prev, canonical), ts),
    )


def _seed(env, old: int = 6, recent: int = 4) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = connect(str(env / "platform.db"))
    for i in range(old):
        _add(conn, (now - timedelta(days=100 - i)).strftime("%Y-%m-%d %H:%M:%S"), f"old_{i}")
    for i in range(recent):
        _add(conn, (now - timedelta(days=2, minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), f"new_{i}")
    conn.commit()
    conn.close()


def _verify():
    from examlops.data.audit import verify_audit_chain

    return verify_audit_chain()


def _prune(env, **kw):
    from examlops.data.audit_retention import execute_prune

    return execute_prune(None, archive_path=str(env / "archive.json"), actor="tester", **kw)


def _raw(env, sql, *args):
    """A privileged raw write: what an attacker with file access would do (drops the triggers)."""
    conn = connect(str(env / "platform.db"))
    for t in ("audit_events_no_update", "audit_events_no_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {t}")
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def test_default_is_keep_forever(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_RETENTION_DAYS")
    _seed(env)
    from examlops.data.audit_retention import RetentionRefused

    with pytest.raises(RetentionRefused, match="kept forever"):
        _prune(env)
    assert _verify()["count"] == 10


def test_dry_run_changes_nothing(env):
    _seed(env)
    res = runner.invoke(app, ["--json", "audit", "prune"])
    assert res.exit_code == 0, res.output
    plan = json.loads(res.stdout)
    assert plan["dry_run"] is True and plan["eligible"] == 6
    assert _verify()["count"] == 10


def test_execute_requires_archive(env):
    _seed(env)
    res = runner.invoke(app, ["audit", "prune", "--execute", "--yes"])
    assert res.exit_code == 1
    assert "--archive" in res.output


def test_prune_then_verify_still_passes(env):
    _seed(env)
    before = _verify()
    assert before["ok"] and before["count"] == 10
    out = _prune(env)
    assert out["status"] == "pruned" and out["pruned"] == 6
    after = _verify()
    assert after["ok"], after
    assert after["pruned_before_id"] == 7
    # 4 retained + the audit_pruned event itself
    assert after["count"] == 5
    with connect(str(env / "platform.db")) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events ORDER BY id")]
    assert actions[-1] == "audit_pruned" and not any(a.startswith("old_") for a in actions)


def test_prune_archives_rows_and_records_digest(env):
    import hashlib

    _seed(env)
    out = _prune(env)
    raw = (env / "archive.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == out["archive_sha256"]
    assert [r["action"] for r in json.loads(raw)] == [f"old_{i}" for i in range(6)]


def test_prune_is_anchored_off_platform(env):
    _seed(env)
    out = _prune(env)
    assert out["worm_hash"]
    from examlops import audit_worm

    assert audit_worm.verify_worm()["ok"]
    lines = (env / "worm.jsonl").read_text().splitlines()
    assert any(json.loads(ln)["key_id"] == "d3-hmac-prune" for ln in lines)


def test_tampered_retained_row_fails_after_prune(env):
    _seed(env)
    _prune(env)
    assert _verify()["ok"]
    _raw(env, "UPDATE audit_events SET action='forged' WHERE action='new_1'")
    res = _verify()
    assert res["ok"] is False and "hash mismatch" in res["reason"]


def test_tampered_first_retained_row_fails_after_prune(env):
    _seed(env)
    _prune(env)
    _raw(env, "UPDATE audit_events SET actor='mallory' WHERE action='new_3'")
    assert _verify()["ok"] is False


def test_forged_prune_record_fails(env):
    _seed(env)
    _prune(env)
    _raw(env, "UPDATE audit_prunes SET cut_hash='deadbeef'")
    res = _verify()
    assert res["ok"] is False and "signature" in res["reason"]


def test_deleted_prune_record_fails(env):
    _seed(env)
    _prune(env)
    _raw(env, "DELETE FROM audit_prunes")
    assert _verify()["ok"] is False  # the retained chain no longer starts at GENESIS


def test_unsigned_prune_record_cannot_be_forged_without_the_key(env, monkeypatch):
    """Re-pointing the cut at another row's hash with a bogus signature is caught."""
    _seed(env)
    _prune(env)
    _raw(env, "UPDATE audit_prunes SET signature='0000'")
    assert _verify()["ok"] is False


def test_refuses_when_chain_already_broken(env):
    _seed(env)
    _raw(env, "UPDATE audit_events SET action='forged' WHERE action='old_2'")
    from examlops.data.audit_retention import RetentionRefused

    with pytest.raises(RetentionRefused, match="does not verify"):
        _prune(env)
    with connect(str(env / "platform.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 10


def test_refuses_without_anchor_unless_allowed(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_WORM_PATH")
    _seed(env)
    from examlops.data.audit_retention import RetentionRefused

    with pytest.raises(RetentionRefused, match="WORM anchor"):
        _prune(env)
    assert _prune(env, allow_unanchored=True)["pruned"] == 6
    assert _verify()["ok"]


def test_refuses_without_signing_key(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY")
    monkeypatch.setattr("examlops.secrets.get_secret", lambda *a, **k: None, raising=False)
    _seed(env)
    from examlops.data.audit_retention import RetentionRefused

    with pytest.raises(RetentionRefused, match="sign"):
        _prune(env)


def test_min_retention_is_a_floor_not_a_suggestion(env):
    """A --before newer than now-retention is clamped: recent rows are never pruned."""
    _seed(env)
    from examlops.data.audit_retention import execute_prune

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    out = execute_prune(tomorrow, archive_path=str(env / "a.json"), actor="tester")
    assert out["pruned"] == 6  # the 4 recent rows survive


def test_second_prune_chains_onto_the_first(env, monkeypatch):
    _seed(env)
    _prune(env)
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "1")  # the 2-day-old rows now qualify
    out = _prune(env)
    assert out["status"] == "pruned" and out["pruned"] >= 4
    res = _verify()
    assert res["ok"], res
    from examlops.data.audit_retention import list_prunes

    assert len(list_prunes()) == 2


def test_cli_prune_execute_end_to_end(env):
    _seed(env)
    res = runner.invoke(
        app, ["--json", "audit", "prune", "--execute", "--archive", str(env / "a.json"), "--yes"]
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["pruned"] == 6
    ver = runner.invoke(app, ["--json", "audit", "verify"])
    assert ver.exit_code == 0 and json.loads(ver.stdout)["ok"] is True


def test_cli_prune_refused_exits_1(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_RETENTION_DAYS")
    res = runner.invoke(app, ["audit", "prune", "--execute", "--archive", "x.json", "--yes"])
    assert res.exit_code == 1 and "refused" in res.output.lower()


def test_delete_trigger_survives_a_prune(env):
    """The append-only trigger is restored: a later plain DELETE is still refused."""
    _seed(env)
    _prune(env)
    with connect(str(env / "platform.db")) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_events WHERE id = (SELECT MAX(id) FROM audit_events)")


def test_failed_prune_leaves_the_trigger_and_rows(env, monkeypatch):
    _seed(env)
    from examlops.data import audit_retention as ar

    real = ar._ensure_table

    def boom(conn):
        real(conn)
        raise RuntimeError("disk full")

    monkeypatch.setattr(ar, "_ensure_table", boom)
    with pytest.raises(RuntimeError):
        _prune(env)
    with connect(str(env / "platform.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 10
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_events WHERE id = 1")
