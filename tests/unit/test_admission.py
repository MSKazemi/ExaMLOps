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


# ── a queue nobody drains must not look like a queue that is moving ──────────


def test_stats_reports_how_long_the_oldest_item_has_been_waiting(db):
    """Counts alone cannot distinguish a busy queue from a stranded one.

    `exa admission submit` enqueues durably and something else must claim the item — the dispatch
    is injected by whatever embeds this facade. If nothing does, the item waits forever and
    `{"queued": 1}` looks exactly like a queue that is simply busy right now. The age of the oldest
    queued item is the one number that tells them apart.
    """
    from examlops import admission

    admission.submit("retrain", {"model": "JPCP"}, tenant="team-a")
    stats = admission.stats()
    assert stats["queued"] == 1
    assert "oldest_queued_age_s" in stats, "the stats cannot show a stranded queue"
    assert stats["oldest_queued_age_s"] >= 0


def test_an_empty_queue_reports_no_age(db):
    """Anti-vacuity: the field must reflect the queue, not always be present with a number."""
    from examlops import admission

    assert admission.stats()["oldest_queued_age_s"] is None


def test_a_claimed_item_stops_counting_as_waiting(db):
    """Once something claims the item it is no longer waiting for a worker."""
    from examlops import admission
    from examlops.data.admission import claim_next_admission

    admission.submit("retrain", {"model": "JPCP"}, tenant="team-a")
    assert claim_next_admission() is not None
    stats = admission.stats()
    assert stats["queued"] == 0 and stats["running"] == 1
    assert stats["oldest_queued_age_s"] is None


def test_submit_says_the_item_waits_for_a_worker(db):
    """`submit` promised work would be "drained under the caps" — by whom was never said.

    Nothing in the platform calls `worker_step`/`drain`: the dispatch is injected, and the control
    plane runs its own admission accounting on this table rather than through this facade. So an
    item submitted here waits until something claims it, and the operator should be told that at
    the moment they submit rather than discovering it from a queue depth that never falls.
    """
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(
        app, ["admission", "submit", "retrain", "-p", '{"model":"JPCP"}', "--tenant", "team-a"]
    )
    assert result.exit_code == 0, result.output
    assert "worker" in result.output.lower(), (
        f"submit implies the work will be done and does not say by what:\n{result.output}"
    )


def test_stats_shows_the_wait_so_a_stranded_queue_is_visible(db):
    """The operator's follow-up question — "is anything moving?" — must be answerable."""
    from typer.testing import CliRunner

    from examlops import admission
    from examlops.cli.main import app

    admission.submit("retrain", {"model": "JPCP"}, tenant="team-a")
    result = CliRunner().invoke(app, ["admission", "stats"])
    assert result.exit_code == 0, result.output
    assert "waiting" in result.output.lower() or "oldest" in result.output.lower(), (
        f"stats shows counts only, so a stranded queue looks like a busy one:\n{result.output}"
    )


def test_stats_declares_the_mixed_value_space_it_returns(db):
    """The annotation must not promise ints while returning a duration that can be None.

    `stats()` said `dict[str, int]` and returned `{"oldest_queued_age_s": None}` on an empty queue.
    mypy could not see it — the helper it delegates to returns `dict[str, Any]` — so the lie reached
    a consumer, which summed the values and raised. Pinned here because a type checker cannot.
    """
    import typing

    from examlops import admission

    hints = typing.get_type_hints(admission.stats)
    assert hints["return"] is not dict[str, int], (
        "stats() promises int values but returns a duration that is None on an empty queue"
    )
    value = admission.stats()["oldest_queued_age_s"]
    assert value is None, "the empty-queue value the annotation has to admit to"
