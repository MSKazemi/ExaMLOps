"""The event backbone reports its backlog, its lag and its dead letters truthfully (plan P2.7).

The outbox guarantees an event is not lost while the broker is down, and a guarantee like that
hides an outage: every write still succeeds. These gauges are what turns "not lost yet" into an
alert, so each is checked in the state it exists to catch — a stalled relay, a lagging consumer, a
parked event — and in the state that must not look like the healthy one: an unreadable store.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _sample(body: str, name: str, labels: str = "") -> float | None:
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        metric, _, value = line.rpartition(" ")
        if metric == name + labels:
            return float(value)
    return None


def _scrape(cp) -> str:
    return TestClient(cp.app).get("/metrics").text


def _seed_outbox(*, pending_since: str | None = None, published: int = 0, poison: int = 0) -> None:
    from examlops.data.events import enqueue_event
    from examlops.platform_db import get_db, init_db

    init_db()
    for _ in range(published):
        enqueue_event("t.done", {})
    if pending_since is not None:
        enqueue_event("t.waiting", {})
    for _ in range(poison):
        enqueue_event("t.poison", {})
    with get_db() as conn:
        conn.execute("UPDATE event_outbox SET published_at=CURRENT_TIMESTAMP WHERE topic='t.done'")
        conn.execute("UPDATE event_outbox SET attempts=99 WHERE topic='t.poison'")
        if pending_since is not None:
            conn.execute(
                "UPDATE event_outbox SET created_at=? WHERE topic='t.waiting'", (pending_since,)
            )


def test_a_stalled_outbox_shows_its_age(cp):
    """A count of 1 looks the same whether it arrived a second or a day ago; the age does not."""
    _seed_outbox(pending_since="2020-01-01 00:00:00", published=3)

    body = _scrape(cp)

    assert _sample(body, "examlops_event_outbox_pending") == 1.0
    assert _sample(body, "examlops_event_outbox_oldest_pending_age_seconds") > 86400


def test_a_drained_outbox_reports_zero_age(cp):
    _seed_outbox(published=2)

    body = _scrape(cp)

    assert _sample(body, "examlops_event_outbox_pending") == 0.0
    assert _sample(body, "examlops_event_outbox_oldest_pending_age_seconds") == 0.0


def test_poison_events_are_counted(cp):
    _seed_outbox(poison=2)
    assert _sample(_scrape(cp), "examlops_event_outbox_poison") == 2.0


def test_an_unreadable_outbox_keeps_the_last_true_values(cp, monkeypatch):
    """Zero backlog is the healthy reading; an unreadable outbox must not produce it."""
    _seed_outbox(pending_since="2020-01-01 00:00:00")
    before = _sample(_scrape(cp), "examlops_event_outbox_pending")
    errors_before = _sample(_scrape(cp), "examlops_metrics_scrape_errors_total") or 0.0

    def _boom(**_kw):
        raise sqlite3.OperationalError("outbox is unreadable")

    monkeypatch.setattr(cp, "_shared_outbox_stats", _boom)
    body = _scrape(cp)

    assert _sample(body, "examlops_event_outbox_pending") == before == 1.0
    assert _sample(body, "examlops_metrics_scrape_errors_total") == errors_before + 1


def test_health_reports_the_oldest_pending_age(cp):
    _seed_outbox(pending_since="2020-01-01 00:00:00")
    outbox = TestClient(cp.app).get("/health").json()["runtime"]["outbox"]
    assert outbox["pending"] == 1 and outbox["oldest_pending_age_seconds"] > 86400


# ─── relay and backbone ───────────────────────────────────────────────────────


def test_relay_outcomes_are_counted(cp):
    before = _sample(_scrape(cp), "examlops_event_relay_events_total", '{outcome="failed"}') or 0.0
    cp._metrics.record_relay({"published": 4, "failed": 1, "claimed": 5})
    body = _scrape(cp)
    assert _sample(body, "examlops_event_relay_events_total", '{outcome="failed"}') == before + 1


class _Backbone:
    def __init__(self, stats=None, error=None):
        self.stats, self.error = stats, error

    def backbone_stats(self):
        if self.error:
            raise self.error
        return self.stats


def _use_backbone(monkeypatch, backbone) -> None:
    from examlops.events import nats_backend

    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.setattr(nats_backend, "shared", lambda: backbone)


def test_consumer_lag_and_dead_letters_come_from_jetstream(cp, monkeypatch):
    _use_backbone(
        monkeypatch,
        _Backbone(
            {
                "consumers": {"autopilot": {"pending": 1500, "ack_pending": 3}},
                "dlq": {"autopilot": 2},
            }
        ),
    )

    cp._refresh_backbone_metrics()
    body = _scrape(cp)

    assert _sample(body, "examlops_event_consumer_pending", '{consumer="autopilot"}') == 1500
    assert _sample(body, "examlops_event_consumer_ack_pending", '{consumer="autopilot"}') == 3
    assert _sample(body, "examlops_event_dlq_messages", '{consumer="autopilot"}') == 2


def test_a_deleted_consumer_stops_being_reported(cp, monkeypatch):
    _use_backbone(monkeypatch, _Backbone({"consumers": {"old": {"pending": 9}}, "dlq": {}}))
    cp._refresh_backbone_metrics()
    _use_backbone(monkeypatch, _Backbone({"consumers": {"new": {"pending": 0}}, "dlq": {}}))
    cp._refresh_backbone_metrics()

    body = _scrape(cp)

    assert _sample(body, "examlops_event_consumer_pending", '{consumer="old"}') is None
    assert _sample(body, "examlops_event_consumer_pending", '{consumer="new"}') == 0


def test_an_unreachable_broker_keeps_the_last_lag(cp, monkeypatch):
    _use_backbone(monkeypatch, _Backbone({"consumers": {"c1": {"pending": 42}}, "dlq": {}}))
    cp._refresh_backbone_metrics()
    _use_backbone(monkeypatch, _Backbone(error=ConnectionError("nats down")))

    cp._refresh_backbone_metrics()

    assert _sample(_scrape(cp), "examlops_event_consumer_pending", '{consumer="c1"}') == 42


def test_without_nats_there_is_no_backbone_to_read(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "log")
    from examlops.events import nats_backend

    def _never():
        raise AssertionError("the log publisher has no broker to ask")

    monkeypatch.setattr(nats_backend, "shared", _never)
    cp._refresh_backbone_metrics()


# Keeping the last lag is right — zero lag is the reading that means "healthy", and an unreachable
# broker must not produce it. But it leaves `EventConsumerLagging` and `EventDeadLettered` judging
# numbers of unknown age, with nothing saying so. The approval gauges on this same endpoint already
# have that signal (`record_scrape_error` → ApprovalMetricsUnreadable); the backbone had none.
def test_an_unreachable_broker_is_counted_not_only_logged(cp, monkeypatch):
    # The registry is process-global, so measure the delta; the series existing *at all* before
    # any failure is the separate claim, and the test below pins it.
    before = _sample(_scrape(cp), "examlops_event_backbone_read_errors_total")

    _use_backbone(monkeypatch, _Backbone(error=ConnectionError("nats down")))
    cp._refresh_backbone_metrics()

    assert _sample(_scrape(cp), "examlops_event_backbone_read_errors_total") == before + 1


def test_the_backbone_error_series_exists_before_any_error(cp):
    """Unlabelled, so prometheus_client exports it from import — an alert cannot fire on a series
    that does not exist yet, which would be exactly the first broker outage."""
    names = [
        line.split()[0]
        for line in _scrape(cp).splitlines()
        if line.startswith("examlops_event_backbone_read_errors_total")
    ]
    assert names, "the counter is absent from a fresh scrape"


def test_a_reachable_broker_does_not_count_an_error(cp, monkeypatch):
    before = _sample(_scrape(cp), "examlops_event_backbone_read_errors_total")
    _use_backbone(monkeypatch, _Backbone({"consumers": {"c1": {"pending": 1}}, "dlq": {}}))
    cp._refresh_backbone_metrics()

    assert _sample(_scrape(cp), "examlops_event_backbone_read_errors_total") == before
