"""ADR 0109 guards: the suspend/resume seam refuses to fake what it cannot do.

Real code paths throughout: a real ``platform.db``, the real provider registry (including a fake
entry point) and a real SQLite file with LangGraph's ``SqliteSaver`` ``checkpoints`` schema (the
DDL below is copied from ``platform/services/agent/agent_memory.db``; LangGraph itself is not
installed in the root venv, so the agent's writer is not exercised here).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from examlops import platform_db
from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events
from examlops.providers import ProviderError, get_provider, list_providers
from examlops.resilience.db import connect
from examlops.suspend import (
    Capability,
    SuspendUnsupported,
    estimate_resume_cost,
    preemption_promise,
    service,
    with_measurements,
)
from examlops.suspend.backends import CheckpointOnlyBackend, MockBackend
from examlops.suspend.checkpoint_store import LangGraphSqliteStore
from examlops.suspend.types import SuspendError

_DDL = """CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"""


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_SUSPEND_BACKEND_PROVIDER", raising=False)
    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


@pytest.fixture
def agent_db(tmp_path, monkeypatch):
    path = tmp_path / "agent_memory.db"
    conn = connect(path)
    conn.execute(_DDL)
    conn.executemany(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint) VALUES (?, ?, ?)",
        [("t1", "0001", b"a" * 10), ("t1", "0002", b"b" * 2048), ("t2", "0001", b"c")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("AGENT_DB", str(path))
    return path


def _actions() -> list[str]:
    with platform_db.get_db() as c:
        return [r[0] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]


# -- capability honesty ------------------------------------------------------------------------


def test_checkpoint_only_capability_reports_unknowns_as_none():
    cap = CheckpointOnlyBackend().capability()
    assert cap.granularity == "application"
    assert cap.gpu_state is False and cap.peer_replication is False
    assert cap.tiers == ("persistent_storage",)
    assert cap.restore_throughput_mb_s is None and cap.communicator_rebuild_s is None
    assert cap.basis == "unknown"


def test_capability_validates_its_vocabulary():
    with pytest.raises(ValueError):
        Capability(backend="x", granularity="gpu-magic")
    with pytest.raises(ValueError):
        Capability(backend="x", granularity="process", basis="guessed")
    with pytest.raises(ValueError):
        Capability(backend="x", granularity="process", tiers=("cloud",))


def test_no_hardware_backend_is_registered_and_it_is_refused_not_faked():
    names = {i.name for i in list_providers("suspend_backend")}
    # Real backends (agent sessions, training checkpoints, the tiered training variant, vLLM's
    # own engine-delegated sleep mode) and a test double — and nothing that claims a
    # process/accelerator-image mechanism (CRIU, cuda-checkpoint).
    assert names == {
        "checkpoint-only",
        "training-checkpoint",
        "tiered-training-checkpoint",
        "vllm-sleep",
        "mock",
    }
    for fake in ("cuda-checkpoint", "criu"):
        with pytest.raises(SuspendUnsupported, match="not built in"):
            service.get_backend(fake)
    with pytest.raises(ProviderError):
        get_provider("suspend_backend", "criu")


def test_unsupported_request_is_refused_and_audited(agent_db):
    with pytest.raises(SuspendUnsupported, match="GPU state"):
        service.suspend("t1", options={"require_gpu_state": True})
    with pytest.raises(SuspendUnsupported, match="cannot persist"):
        service.suspend("t1", subject_kind="training_job")
    assert _actions() == ["suspend_refused", "suspend_refused"]
    assert service.list_snapshots() == []  # nothing recorded for a refused suspend


def test_a_fake_entry_point_backend_is_discovered_and_used(monkeypatch):
    import importlib.metadata as md

    class Fake(MockBackend):
        name = "fake-gpu"

        def __init__(self):
            super().__init__(
                Capability(
                    backend="fake-gpu",
                    granularity="accelerator_state",
                    state_kinds=("gpu_job",),
                    gpu_state=True,
                    communicator_rebuild_applicable=True,
                    basis="declared",
                )
            )

    ep = md.EntryPoint("fake-gpu", f"{__name__}:_FakeCls", "exa.providers.suspend_backend")
    monkeypatch.setitem(globals(), "_FakeCls", Fake)
    from examlops.providers import registry

    real = registry._entry_points
    monkeypatch.setattr(
        registry,
        "_entry_points",
        lambda g: [ep] if g == "exa.providers.suspend_backend" else real(g),
    )
    assert "fake-gpu" in {i.name for i in list_providers("suspend_backend") if i.ok}
    cap = service.capability("fake-gpu")
    assert cap.gpu_state and cap.communicator_rebuild_s is None  # unknown stays None


# -- suspend -> resume round trip on the checkpoint-only backend -------------------------------


def test_round_trip_records_pointer_audits_and_reports_timing_split(agent_db):
    h = service.suspend("t1", actor="alice")
    assert h.pointer == {"checkpoint_ns": "", "checkpoint_id": "0002"}  # newest, pinned
    assert h.state_bytes == 2048
    row = service.status(h.snapshot_id)
    assert row["status"] == "suspended" and row["pointer"]["checkpoint_id"] == "0002"
    assert row["capability"]["granularity"] == "application"

    report = service.resume(h.snapshot_id, actor="alice")
    assert report.restored and report.state_transfer_s is not None and report.state_transfer_s >= 0
    assert report.communicator_rebuild_s == 0.0  # no communicator exists: a fact, not an estimate
    row = service.status(h.snapshot_id)
    assert row["status"] == "resumed" and row["state_transfer_s"] is not None
    assert _actions() == ["suspend_snapshot", "suspend_resume"]
    assert not dropped_audit_events()  # both events landed

    with pytest.raises(SuspendError, match="not suspended"):
        service.resume(h.snapshot_id)  # no double resume


def test_resume_fails_visibly_when_the_pinned_checkpoint_is_gone(agent_db):
    h = service.suspend("t2")
    conn = connect(agent_db)
    conn.execute("DELETE FROM checkpoints WHERE thread_id='t2'")
    conn.commit()
    conn.close()
    report = service.resume(h.snapshot_id)
    assert not report.restored
    assert service.status(h.snapshot_id)["status"] == "failed"
    assert "suspend_resume_failed" in _actions()


def test_nothing_to_suspend_is_an_error_and_the_store_is_not_written(agent_db):
    with pytest.raises(SuspendError, match="no checkpoint exists"):
        service.suspend("no-such-thread")
    conn = connect(agent_db)
    assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 3
    conn.close()


def test_missing_store_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_DB", raising=False)
    with pytest.raises(SuspendError, match="AGENT_DB"):
        service.suspend("t1")
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "absent.db"))
    with pytest.raises(SuspendError, match="not found"):
        service.suspend("t1")


def test_discard_releases_the_record_but_not_the_agents_checkpoint(agent_db):
    h = service.suspend("t1")
    service.discard(h.snapshot_id)
    assert service.status(h.snapshot_id)["status"] == "discarded"
    assert LangGraphSqliteStore(str(agent_db)).latest("t1") is not None
    assert "suspend_discard" in _actions()


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_suspend_audit_is_counted(agent_db, monkeypatch):
    _break_audit(monkeypatch)
    service.suspend("t1")  # the operation still succeeds
    assert dropped_audit_events()


def test_a_lost_resume_audit_is_counted(agent_db, monkeypatch):
    h = service.suspend("t1")
    reset_dropped_audit_events()
    _break_audit(monkeypatch)
    assert service.resume(h.snapshot_id).restored
    assert dropped_audit_events()


def test_a_lost_discard_audit_is_counted(agent_db, monkeypatch):
    h = service.suspend("t1")
    reset_dropped_audit_events()
    _break_audit(monkeypatch)
    service.discard(h.snapshot_id)
    assert dropped_audit_events()


def test_store_is_read_only(agent_db):
    store = LangGraphSqliteStore(str(agent_db))
    ref = store.latest("t1")
    assert ref is not None
    import examlops.resilience.db as rdb

    ro = rdb.connect_snapshot(str(agent_db))
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM checkpoints")
    ro.close()


# -- resume-cost model -------------------------------------------------------------------------


def test_cost_is_unknown_when_inputs_are_unknown():
    cap = CheckpointOnlyBackend().capability()
    c = estimate_resume_cost(cap, 10 * 1024 * 1024)
    assert c.total_s is None and c.basis == "unknown"
    assert estimate_resume_cost(replace(cap, restore_throughput_mb_s=10.0), None).basis == "unknown"


def test_cost_basis_labels_and_arithmetic():
    cap = replace(
        CheckpointOnlyBackend().capability(), restore_throughput_mb_s=10.0, restore_fixed_s=1.0
    )
    declared = replace(cap, basis="declared")
    c = estimate_resume_cost(declared, 50 * 1024 * 1024)
    assert c.total_s == pytest.approx(6.0) and c.basis == "declared"
    assert c.communicator_rebuild_s == 0.0  # not applicable
    measured = estimate_resume_cost(replace(cap, basis="measured"), 50 * 1024 * 1024)
    assert measured.basis == "measured"


def test_applicable_but_unmeasured_communicator_makes_the_total_unknown():
    cap = Capability(
        backend="x",
        granularity="accelerator_state",
        state_kinds=("gpu_job",),
        communicator_rebuild_applicable=True,
        restore_throughput_mb_s=100.0,
        basis="declared",
    )
    c = estimate_resume_cost(cap, 1024 * 1024)
    assert c.total_s is None and c.basis == "unknown" and c.state_transfer_s is not None
    known = estimate_resume_cost(replace(cap, communicator_rebuild_s=20.0), 100 * 1024 * 1024)
    assert known.total_s == pytest.approx(21.0) and known.basis == "declared"


def test_measurement_only_comes_from_recorded_restores(agent_db):
    base = service.capability()
    assert base.basis == "unknown"
    assert with_measurements(base, [{"state_bytes": None, "state_transfer_s": None}]) is base
    h = service.suspend("t1")
    service.resume(h.snapshot_id)
    cap = service.capability()
    assert cap.basis == "measured" and cap.restore_throughput_mb_s
    assert estimate_resume_cost(cap, 1024 * 1024).basis == "measured"


# -- consumers degrade rather than pretend -----------------------------------------------------


def test_preemption_promise_declines_and_says_why():
    none_cap = Capability(backend="x", granularity="process")
    p = preemption_promise(none_cap)
    assert not p.can_promise and p.reasons
    ok = preemption_promise(CheckpointOnlyBackend().capability())
    assert ok.can_promise
    assert any("application granularity" in r for r in ok.reasons)  # the ceiling stays visible


def test_domain_is_listed_by_the_cli():
    import json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    res = CliRunner().invoke(app, ["--json", "providers", "list", "--domain", "suspend_backend"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert any(r["name"] == "checkpoint-only" and r["default"] for r in rows)
