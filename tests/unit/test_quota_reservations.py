"""ADR 0116 decision 3 - two-phase quota reservation on a real platform.db."""

from __future__ import annotations

import threading

import pytest

from examlops.admission_seam import JobRequest, Resources
from examlops.admission_seam import reservations as svc
from examlops.data import quota_reservations as store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_RESERVATION_TTL_S", raising=False)
    import examlops.platform_db as pdb
    from examlops.data.audit import reset_dropped_audit_events

    pdb.init_db()
    reset_dropped_audit_events()
    return pdb


def _r(rid, gpus=2, hours=0.0, limit=4, ttl=100.0, now=1000.0, hours_limit=None):
    return store.reserve(
        rid,
        "p",
        tenant="t",
        gpus=gpus,
        gpu_hours=hours,
        ttl_s=ttl,
        gpus_limit=limit,
        gpu_hours_limit=hours_limit,
        now=now,
    )


def test_reserve_commit_release_lifecycle(db):
    assert _r("r1")["ok"]
    assert store.held_totals("p", now=1001.0)["gpus"] == 2
    assert store.commit("r1", now=1001.0)
    assert store.get("r1")["state"] == "committed"
    assert store.held_totals("p", now=5000.0)["gpus"] == 2  # committed never lapses
    assert store.release("r1", reason="job done", now=1002.0)
    assert store.held_totals("p", now=1003.0)["gpus"] == 0
    assert not store.release("r1"), "a second release changes nothing"
    assert not store.commit("r1"), "a released reservation cannot be committed"


def test_reservations_count_against_the_gpu_limit(db):
    assert _r("a", gpus=3)["ok"]
    out = _r("b", gpus=2)
    assert not out["ok"] and "limit 4" in out["reason"]
    assert store.get("b") is None
    assert _r("c", gpus=1)["ok"]  # exactly the limit


def test_gpu_hours_are_a_second_limit(db):
    assert _r("a", gpus=1, hours=6.0, limit=None, hours_limit=10.0)["ok"]
    out = _r("b", gpus=1, hours=5.0, limit=None, hours_limit=10.0)
    assert not out["ok"] and "gpu-hours" in out["reason"]


def test_a_leaked_reservation_expires_visibly_and_frees_the_quota(db):
    assert _r("leak", gpus=4, ttl=10.0, now=1000.0)["ok"]
    assert not _r("blocked", gpus=1, now=1005.0)["ok"]  # still held before the TTL
    preview = store.expire_due(now=1011.0, dry_run=True)
    assert [r["id"] for r in preview] == ["leak"]
    assert store.get("leak")["state"] == "reserved", "a preview changes nothing"
    assert store.held_totals("p", now=1011.0)["gpus"] == 0, "lapsed holds nothing even unswept"
    assert not store.commit("leak", now=1011.0), "a lapsed reservation cannot be committed"
    assert _r("after", gpus=4, now=1011.0)["ok"]  # reserve sweeps in the same transaction
    row = store.get("leak")
    assert row["state"] == "expired" and row["reason"] == "ttl elapsed"
    assert store.list_reservations(state="expired")[0]["id"] == "leak"


def test_concurrent_reservers_cannot_both_take_the_last_slot(db):
    n, wins, barrier = 8, [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        out = store.reserve(f"c{i}", "p", tenant="t", gpus=1, ttl_s=100.0, gpus_limit=3, now=1000.0)
        wins.append(out["ok"])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(wins) == 3, wins
    assert store.held_totals("p", now=1000.0)["gpus"] == 3


def test_service_uses_project_limits_audits_and_counts_a_lost_audit(db, monkeypatch):
    from examlops.data.audit import dropped_audit_events
    from examlops.data.projects import create_project

    create_project("proj", gpu_limit=4)
    req = JobRequest(project="proj", tenant="t", resources=Resources(gpus=3), est_runtime_s=3600)
    ok = svc.reserve(req)
    assert ok["ok"] and ok["reservation"]["gpus"] == 3
    refused = svc.reserve(req)
    assert not refused["ok"] and "limit 4" in refused["reason"]
    assert svc.commit(ok["reservation"]["id"]) and svc.release(ok["reservation"]["id"])

    with db.get_db() as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE source='admission-seam' ORDER BY id"
            )
        ]
    assert actions == [
        "quota_reserved",
        "quota_reservation_refused",
        "quota_committed",
        "quota_released",
    ]

    from examlops.data import audit as audit_mod

    monkeypatch.setattr(
        audit_mod, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    svc.reserve(JobRequest(project="proj", tenant="t", resources=Resources(gpus=1)))
    assert dropped_audit_events(), "a lost reservation audit must be counted, not silent"


def test_service_gpu_hours_headroom_comes_from_the_budget(db):
    from examlops.data.projects import create_project, set_project_budget

    create_project("hb")  # default gpu_limit 0 = no concurrency quota
    set_project_budget("hb", 10.0, None)
    big = JobRequest(project="hb", resources=Resources(gpus=4), est_runtime_s=3 * 3600)  # 12 h
    assert not svc.reserve(big)["ok"]
    fits = JobRequest(project="hb", resources=Resources(gpus=2), est_runtime_s=3 * 3600)  # 6 h
    assert svc.reserve(fits)["ok"]
    assert not svc.reserve(fits)["ok"]  # 6 held + 6 > 10


def test_decide_audits_only_the_non_default_policy(db):
    from examlops.admission_seam.policy import ClusterState, Quotas
    from examlops.admission_seam.service import decide

    req = JobRequest(project="p", tenant="a", resources=Resources(gpus=1))
    st, q = ClusterState(free_gpus=4), Quotas()
    decide(req, state=st, quotas=q)
    decide(req, state=st, quotas=q, policy="baseline-over-quota")

    def count():
        with db.get_db() as conn:
            return conn.execute(
                "SELECT COUNT(*) c FROM audit_events WHERE action='admission_decision'"
            ).fetchone()["c"]

    assert count() == 1


def test_a_lost_admission_decision_audit_is_counted(db, monkeypatch):
    from examlops.admission_seam.policy import ClusterState, Quotas
    from examlops.admission_seam.service import decide
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events

    monkeypatch.setattr(
        audit_mod, "write_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    req = JobRequest(project="p", tenant="a", resources=Resources(gpus=1))
    decision, _ = decide(
        req, state=ClusterState(free_gpus=4), quotas=Quotas(), policy="baseline-over-quota"
    )
    assert decision.verdict == "admit"  # the decision is unaffected by the lost record
    assert "admission_decision" in dropped_audit_events()
