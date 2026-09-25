"""Scheduled audit maintenance (ADR 0028): the periodic export, checkpoint and retention job.

Real code end to end: the real append-only chain and triggers, the real WORM anchor file, the
real retention module and the real DB-backed coordinator lease. Only time is steered (old rows are
written with explicit timestamps).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from examlops import audit_maintenance as am
from examlops import audit_transparency as at
from examlops.cli.main import app
from examlops.resilience.db import connect

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUDIT_WORM_PATH", str(tmp_path / "worm.jsonl"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    monkeypatch.setenv("EXAMLOPS_AUDIT_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "db")
    for var in (
        "EXAMLOPS_AUDIT_PRUNE_SCHEDULED",
        "EXAMLOPS_AUDIT_RETENTION_DAYS",
        "EXAMLOPS_AUDIT_REKOR_URL",
        "EXAMLOPS_AUDIT_TRANSPARENCY",
        "EXAMLOPS_AUDIT_MAINTENANCE_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops.coordination import reset_coordinator
    from examlops.platform_db import init_db

    reset_coordinator()
    init_db()
    yield tmp_path
    at.set_transport(None)


def _add(conn, ts: str, action: str) -> None:
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


def _seed(env, old: int = 5, recent: int = 3) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = connect(str(env / "platform.db"))
    for i in range(old):
        _add(conn, (now - timedelta(days=100 - i)).strftime("%Y-%m-%d %H:%M:%S"), f"old_{i}")
    for i in range(recent):
        _add(conn, (now - timedelta(hours=recent - i)).strftime("%Y-%m-%d %H:%M:%S"), f"new_{i}")
    conn.commit()
    conn.close()


def _count(sql: str) -> int:
    from examlops.platform_db import get_db

    with get_db() as conn:
        return int(conn.execute(sql).fetchone()[0])


def test_a_cycle_signs_and_anchors_the_head_then_is_a_no_op(env):
    _seed(env)
    first = am.run_cycle()
    assert first["status"] == "ok"
    assert first["checkpoint"]["status"] == "checkpointed" and first["checkpoint"]["anchored"]
    assert first["prune"] == {"status": "disabled"}
    from examlops.audit_worm import verify_worm

    assert verify_worm()["ok"] is True and verify_worm()["entries"] == 1
    second = am.run_cycle()
    assert second["checkpoint"]["status"] == "unchanged"
    assert _count("SELECT COUNT(*) FROM audit_checkpoints") == 1
    runs = am.list_runs()
    assert [r["status"] for r in runs] == ["ok", "ok"]
    assert runs[0]["result"]["checkpoint"]["status"] == "unchanged"


def test_no_signing_key_is_a_degraded_cycle_not_a_crash(env, monkeypatch):
    _seed(env)
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY")
    before = am.step_failures()
    res = am.run_cycle()
    assert res["status"] == "degraded" and res["failed_steps"] == ["checkpoint"]
    assert res["checkpoint"]["status"] == "unconfigured"
    assert am.step_failures() == before + 1
    assert _count("SELECT COUNT(*) FROM audit_checkpoints") == 0
    assert am.list_runs()[0]["status"] == "degraded"


def test_a_transparency_outage_degrades_the_cycle(env, monkeypatch):
    _seed(env)
    monkeypatch.setenv("EXAMLOPS_AUDIT_REKOR_URL", "https://rekor.example.test")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (env / "tlog.pem").write_bytes(pem)
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE", str(env / "tlog.pem"))

    def down(*_a, **_k):
        raise ConnectionError("rekor unreachable")

    at.set_transport(down)
    res = am.run_cycle()
    assert res["status"] == "degraded"
    assert "unreachable" in res["checkpoint"]["transparency_error"]
    # The checkpoint and the WORM entry still happened: the outage is one step, not the job.
    assert _count("SELECT COUNT(*) FROM audit_checkpoints") == 1


def test_the_lease_allows_one_cycle_at_a_time(env):
    from examlops.coordination import get_coordinator

    assert get_coordinator().try_lock(am.LEASE_KEY, "other-host:1", 600)
    res = am.run_cycle(holder="me:2")
    assert res["status"] == "skipped"
    assert _count("SELECT COUNT(*) FROM audit_maintenance_runs") == 0
    get_coordinator().unlock(am.LEASE_KEY, "other-host:1")
    assert am.run_cycle(holder="me:2")["status"] == "ok"
    # The lease is released after the cycle, so the next holder is not locked out.
    assert get_coordinator().try_lock(am.LEASE_KEY, "other-host:1", 600)


def test_prune_is_opt_in(env, monkeypatch):
    _seed(env)
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "30")
    am.run_cycle()
    assert _count("SELECT COUNT(*) FROM audit_events") >= 8  # nothing deleted without opt-in


def test_scheduled_prune_keeps_the_chain_verifiable(env, monkeypatch):
    _seed(env, old=5, recent=3)
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("EXAMLOPS_AUDIT_PRUNE_SCHEDULED", "1")
    res = am.run_cycle()
    assert res["status"] == "ok", res
    assert res["prune"]["status"] == "pruned" and res["prune"]["pruned"] == 5
    archive = res["prune"]["archive"]
    assert [e["action"] for e in json.loads(open(archive).read())] == [f"old_{i}" for i in range(5)]
    from examlops.data.audit import verify_audit_chain

    assert verify_audit_chain()["ok"] is True
    assert _count("SELECT COUNT(*) FROM audit_events WHERE action = 'audit_pruned'") == 1
    assert _count("SELECT COUNT(*) FROM audit_events WHERE action LIKE 'old_%'") == 0
    # Nothing older than the floor is left, so the next cycle prunes nothing.
    assert am.run_cycle()["prune"]["status"] == "nothing-to-prune"


def test_scheduled_prune_without_a_policy_is_reported(env, monkeypatch):
    _seed(env)
    monkeypatch.setenv("EXAMLOPS_AUDIT_PRUNE_SCHEDULED", "1")
    res = am.run_cycle()
    assert res["status"] == "degraded" and res["prune"]["status"] == "no-retention-policy"


def test_a_refused_prune_deletes_nothing(env, monkeypatch):
    _seed(env)
    monkeypatch.delenv("EXAMLOPS_AUDIT_WORM_PATH")  # the prune demands an off-platform anchor
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("EXAMLOPS_AUDIT_PRUNE_SCHEDULED", "1")
    res = am.run_cycle()
    assert res["prune"]["status"] == "refused" and "WORM" in res["prune"]["reason"]
    assert _count("SELECT COUNT(*) FROM audit_events WHERE action LIKE 'old_%'") == 5


def test_dry_run_changes_nothing(env, monkeypatch):
    _seed(env)
    monkeypatch.setenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("EXAMLOPS_AUDIT_PRUNE_SCHEDULED", "1")
    res = am.run_cycle(dry_run=True)
    assert res["status"] == "dry-run"
    assert res["checkpoint"]["status"] == "would-checkpoint"
    assert res["prune"]["status"] == "would-prune" and res["prune"]["eligible"] == 5
    assert _count("SELECT COUNT(*) FROM audit_checkpoints") == 0
    assert _count("SELECT COUNT(*) FROM audit_maintenance_runs") == 0
    assert _count("SELECT COUNT(*) FROM audit_events WHERE action LIKE 'old_%'") == 5


def test_the_run_history_is_bounded(env, monkeypatch):
    monkeypatch.setattr(am, "MAX_RUNS_KEPT", 3)
    for _ in range(5):
        am.run_cycle()
    assert _count("SELECT COUNT(*) FROM audit_maintenance_runs") == 3


def test_interval_parsing_is_bounded(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", raising=False)
    assert am.interval_seconds() == am.DEFAULT_INTERVAL_S
    monkeypatch.setenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "0")
    assert am.interval_seconds() == 0.0
    monkeypatch.setenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "1")
    assert am.interval_seconds() == am.MIN_INTERVAL_S
    monkeypatch.setenv("EXAMLOPS_AUDIT_MAINTENANCE_SECONDS", "junk")
    assert am.interval_seconds() == am.DEFAULT_INTERVAL_S


def test_run_forever_runs_until_stopped(env, monkeypatch):
    calls: list[int] = []
    stop = threading.Event()

    def fake_cycle():
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        return {"status": "ok"}

    monkeypatch.setattr(am, "run_cycle", fake_cycle)
    t = threading.Thread(target=am.run_forever, args=(stop,), kwargs={"interval": 0.01})
    t.start()
    t.join(timeout=5)
    assert not t.is_alive() and len(calls) == 2


def test_run_forever_survives_a_crashing_cycle(env, monkeypatch):
    stop = threading.Event()
    seen: list[int] = []

    def boom():
        seen.append(1)
        if len(seen) == 3:
            stop.set()
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(am, "run_cycle", boom)
    before = am.step_failures()
    am.run_forever(stop, interval=0.01)
    assert len(seen) == 3 and am.step_failures() == before + 3


def test_run_forever_disabled_returns_at_once(env, monkeypatch):
    cycles = []
    monkeypatch.setattr(am, "run_cycle", lambda *a, **k: cycles.append(1))
    stop = threading.Event()  # never set: only the disabled schedule can make it return
    worker = threading.Thread(target=am.run_forever, args=(stop,), kwargs={"interval": 0})
    worker.daemon = True
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "a disabled schedule (interval=0) must return, not loop"
    assert cycles == [], "a disabled schedule must not run a maintenance cycle"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def test_cli_maintain_once_and_history(env):
    _seed(env)
    r = runner.invoke(app, ["--json", "audit", "maintain", "--once"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["checkpoint"]["status"] == "checkpointed"
    r = runner.invoke(app, ["--json", "audit", "maintenance-runs"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.stdout)
    assert len(rows) == 1 and rows[0]["status"] == "ok"


def test_cli_maintain_exits_1_when_degraded(env, monkeypatch):
    _seed(env)
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY")
    r = runner.invoke(app, ["--json", "audit", "maintain", "--once"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["status"] == "degraded"


def test_cli_maintain_dry_run(env):
    _seed(env)
    r = runner.invoke(app, ["--json", "audit", "maintain", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["status"] == "dry-run"
    assert _count("SELECT COUNT(*) FROM audit_checkpoints") == 0
