"""Admission-control queue with per-tenant fair-share (enterprise-readiness Phase 1, item 1.5).

Proves the anti-starvation guarantee: a global concurrency cap is respected, one tenant can't
exceed its per-tenant cap, dequeue order is max-min fair across tenants (not FIFO — a tenant that
dumped 100 items can't monopolize the cluster), priority orders within a tenant, and crashed
'running' items are reclaimed.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_global_concurrency_cap(db):
    for i in range(5):
        db.enqueue_admission("retrain", {"i": i}, tenant="t1")
    claimed = []
    while (item := db.claim_next_admission(max_running=3, per_tenant_cap=10)) is not None:
        claimed.append(item)
    assert len(claimed) == 3  # global cap stops at 3 concurrently running
    assert db.admission_stats()["running"] == 3
    assert db.admission_stats()["queued"] == 2


def test_per_tenant_cap(db):
    for i in range(5):
        db.enqueue_admission("retrain", {"i": i}, tenant="t1")
    a = db.claim_next_admission(max_running=10, per_tenant_cap=2)
    b = db.claim_next_admission(max_running=10, per_tenant_cap=2)
    c = db.claim_next_admission(max_running=10, per_tenant_cap=2)
    assert a and b and c is None  # t1 hits its per-tenant cap of 2


def test_fair_share_across_tenants(db):
    # t1 floods 10 items; t2 submits 1. Fair-share must not let t1 monopolize.
    for i in range(10):
        db.enqueue_admission("retrain", {"i": i}, tenant="t1")
    db.enqueue_admission("retrain", {"i": 0}, tenant="t2")

    first = db.claim_next_admission(max_running=10, per_tenant_cap=10)
    second = db.claim_next_admission(max_running=10, per_tenant_cap=10)
    tenants = {first["tenant"], second["tenant"]}
    # Both tenants get a slot in the first two claims despite t1's flood (max-min fairness).
    assert tenants == {"t1", "t2"}


def test_priority_within_tenant(db):
    db.enqueue_admission("retrain", {"i": "low"}, tenant="t1", priority=0)
    db.enqueue_admission("retrain", {"i": "high"}, tenant="t1", priority=5)
    item = db.claim_next_admission(max_running=10, per_tenant_cap=10)
    import json

    assert json.loads(item["payload"])["i"] == "high"


def test_complete_frees_a_slot(db):
    for i in range(3):
        db.enqueue_admission("retrain", {"i": i}, tenant="t1")
    a = db.claim_next_admission(max_running=1, per_tenant_cap=10)
    assert db.claim_next_admission(max_running=1, per_tenant_cap=10) is None  # cap reached
    db.complete_admission(a["id"], state="done")
    assert db.claim_next_admission(max_running=1, per_tenant_cap=10) is not None  # slot freed


def test_crashed_running_item_is_reclaimed(db):
    db.enqueue_admission("retrain", {"i": 0}, tenant="t1")
    a = db.claim_next_admission(max_running=1, per_tenant_cap=1)
    assert a is not None
    # Simulate a crash long ago: backdate started_at beyond the reclaim window.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE admission_queue SET started_at = datetime(CURRENT_TIMESTAMP, '-2 hours') "
            "WHERE id=?",
            (a["id"],),
        )
    reclaimed = db.claim_next_admission(max_running=1, per_tenant_cap=1, reclaim_after_s=3600)
    assert reclaimed is not None and reclaimed["id"] == a["id"]


def test_worker_step_dispatches_and_completes(db):
    from examlops import admission

    admission.submit("retrain", {"model": "JPCP"}, tenant="t1")
    seen = []
    r = admission.worker_step(lambda item: seen.append(item), max_running_=4, per_tenant_cap_=2)
    assert r["ok"] is True and len(seen) == 1
    assert admission.stats()["done"] == 1


def test_worker_step_marks_failed_on_dispatch_error(db):
    from examlops import admission

    admission.submit("retrain", {"model": "JPCP"}, tenant="t1")

    def _boom(item):
        raise RuntimeError("prefect down")

    r = admission.worker_step(_boom, max_running_=4, per_tenant_cap_=2)
    assert r["ok"] is False and "prefect down" in r["error"]
    assert admission.stats()["failed"] == 1


def test_concurrent_claims_respect_cap(db):
    for i in range(20):
        db.enqueue_admission("retrain", {"i": i}, tenant="t1")
    claimed: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        item = db.claim_next_admission(max_running=5, per_tenant_cap=10)
        if item is not None:
            with lock:
                claimed.append(item["id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 5  # global cap honored under concurrency
    assert len(set(claimed)) == 5  # no item claimed twice
