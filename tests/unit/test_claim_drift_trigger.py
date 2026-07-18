"""Atomic cooldown claim for auto-retrain (enterprise-readiness Phase 0, item 0.12 / QW7).

`claim_drift_trigger` replaces the read-cooldown-then-stamp pattern that let two overlapping
autopilot / `drift trigger` cycles both pass the cooldown check and double-fire a retrain. The
check-and-stamp is now a single conditional UPDATE, so at most one caller can claim a model per
cooldown window — proven here under concurrency.
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


def test_first_claim_succeeds_second_blocked(db):
    db.set_drift_auto_retrain("JPCP", enabled=True, cooldown_s=3600)
    assert db.claim_drift_trigger("JPCP", 3600) is True  # no prior trigger → claim
    assert db.claim_drift_trigger("JPCP", 3600) is False  # within cooldown → blocked


def test_zero_cooldown_always_claims(db):
    db.set_drift_auto_retrain("JPCP", enabled=True, cooldown_s=0)
    assert db.claim_drift_trigger("JPCP", 0) is True
    assert db.claim_drift_trigger("JPCP", 0) is True  # no cooldown → repeatable


def test_unknown_model_not_claimed(db):
    assert db.claim_drift_trigger("NOPE", 3600) is False


def test_exactly_one_winner_under_concurrency(db):
    """16 threads race to claim the same model with a long cooldown → exactly one wins."""
    db.set_drift_auto_retrain("JPCP", enabled=True, cooldown_s=3600)
    n = 16
    barrier = threading.Barrier(n)
    wins: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        got = db.claim_drift_trigger("JPCP", 3600)
        with lock:
            wins.append(got)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(wins) == 1, f"expected exactly one winner, got {sum(wins)}"
