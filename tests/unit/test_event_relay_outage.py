"""An unreachable broker must cost one timeout, not one per event (plan P5, chaos drill).

The combined-failure section of `tests/integration/test_backbone_outage_drill_live.py` measured
what the relay actually did with NATS dead and six events queued: it claimed all six, then failed
them **one at a time** at the client's 5 s timeout each, so a single cycle took 36 seconds. For all
of those 36 seconds `/health` published the *previous* cycle's result — `ok`, with six events
sitting in the outbox. At the default batch of 100 that is over eight minutes of a dead backbone
looking healthy.

Two things were wrong, and they are separate:

- **cost.** A cycle against a broker that is not there must stop at the first transport failure.
  The remaining rows are not "bad"; there is simply nowhere to send them, and trying each one
  proves it again at the price of another timeout.
- **the retry budget.** `attempts` exists to stop a *poison event* — one the broker rejects and
  always will. An outage spent that budget too, so a broker away for five cycles stranded every
  queued event as poison, permanently, with no operator action able to distinguish the two.

So a transport failure now defers the rest of the batch without charging an attempt, and says so
in the relay result, which is what `/health` reports.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", raising=False)
    import examlops.events as events
    from examlops.data import events as db_events
    from examlops.data import init_db

    init_db()
    events.reset_publisher()
    yield db_events
    events.reset_publisher()


class _Unreachable:
    """A broker that is not there: every call ends in a timeout, never an answer.

    ``limit`` bounds how long it plays that part, so a regression in the relay's loop control
    fails the test instead of hanging the suite: past it the failure stops looking like an
    outage, which lets the retry budget end the loop.
    """

    def __init__(self, limit: int = 1000) -> None:
        self.calls = 0
        self._limit = limit

    def publish(self, topic, payload, *, event_id):
        self.calls += 1
        if self.calls > self._limit:
            raise RuntimeError("the relay kept asking an absent broker")
        raise TimeoutError("nats: no servers available for connection")


def _last_error(db) -> str | None:
    """What the outbox recorded against the oldest unpublished row."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT last_error FROM event_outbox WHERE published_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
    return row["last_error"] if row else None


def _queue(events, count: int) -> None:
    for i in range(count):
        events.publish("drift.detected", {"model": "JPCP", "i": i})


# ── the cost of a cycle ───────────────────────────────────────────────────────


def test_a_cycle_stops_at_the_first_unreachable_answer(db):
    """Six events against a dead broker is one timeout, not six."""
    import examlops.events as events

    publisher = _Unreachable()
    events._publisher = publisher
    _queue(events, 6)

    result = events.relay_once()

    assert publisher.calls == 1, f"the relay tried {publisher.calls} events at a timeout each"
    assert result["claimed"] == 6
    assert result["published"] == 0
    assert result["failed"] == 0, "an absent broker is not six failed events"
    assert result["deferred"] == 6, "the whole batch waits for the broker, and says so"
    assert "no servers" in result["unavailable"]


def test_the_deferred_events_are_still_there_and_still_pending(db):
    import examlops.events as events

    events._publisher = _Unreachable()
    _queue(events, 3)
    events.relay_once()

    stats = db.outbox_stats()
    assert stats == {"pending": 3, "published": 0, "poison": 0}


def test_an_outage_does_not_spend_the_retry_budget(db, monkeypatch):
    """The budget is for an event the broker refuses. A broker that is absent refuses nothing —
    and if the outage spends it, a five-cycle outage strands the backlog forever."""
    import examlops.events as events

    monkeypatch.setenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "2")
    events._publisher = _Unreachable()
    _queue(events, 2)

    for _ in range(10):  # five times the budget
        events.relay_once()
    assert db.outbox_stats() == {"pending": 2, "published": 2 * 0, "poison": 0}

    # And the backlog goes out untouched when the broker comes back — no operator step in between.
    published: list[str] = []

    class _Back:
        def publish(self, topic, payload, *, event_id):
            published.append(event_id)

    events._publisher = _Back()
    result = events.relay_once()
    assert result["published"] == 2 and len(published) == 2
    assert db.outbox_stats()["pending"] == 0


def test_a_row_the_broker_rejected_still_counts_and_still_poisons(db, monkeypatch):
    """The deferral must not swallow a genuinely bad event: that is what the budget is for."""
    import examlops.events as events

    monkeypatch.setenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "2")

    class _Rejects:
        def publish(self, topic, payload, *, event_id):
            raise ValueError("subject is longer than the broker accepts")

    events._publisher = _Rejects()
    _queue(events, 1)

    assert events.relay_once() == {"claimed": 1, "published": 0, "failed": 1}
    assert events.relay_once() == {"claimed": 1, "published": 0, "failed": 1}
    assert events.relay_once() == {"claimed": 0, "published": 0, "failed": 0}
    assert db.outbox_stats()["poison"] == 1


def test_the_events_before_the_outage_are_published_first(db):
    """A broker that dies mid-batch keeps what it already took: only the rest is deferred."""
    import examlops.events as events

    class _DiesAfterTwo:
        def __init__(self) -> None:
            self.calls = 0

        def publish(self, topic, payload, *, event_id):
            self.calls += 1
            if self.calls > 2:
                raise ConnectionError("connection refused")

    events._publisher = _DiesAfterTwo()
    _queue(events, 5)

    result = events.relay_once()
    assert result["published"] == 2 and result["deferred"] == 3 and result["failed"] == 0
    assert db.outbox_stats() == {"pending": 3, "published": 2, "poison": 0}


# ── telling an outage apart from a bad event ──────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "unavailable"),
    [
        (TimeoutError("nats: no servers available for connection"), True),
        (ConnectionError("connection refused"), True),
        (OSError("[Errno 113] No route to host"), True),
        (RuntimeError("nats: connection closed"), True),
        (RuntimeError("timed out waiting for a JetStream ack"), True),
        # redis-py's own wording, which is not a builtin ConnectionError
        (RuntimeError("Error 111 connecting to redis:6379. Connection refused."), True),
        (ValueError("payload is not JSON-serialisable"), False),
        (RuntimeError("subject is invalid"), False),
        (RuntimeError("broker down"), False),  # says nothing about the transport
        # A publisher that is not wired up is not a transport failure *by its text*: the skeleton
        # publisher says so itself (below), which is why the generic signs must not guess it.
        (
            RuntimeError("kafka event publisher is not configured — set EXAMLOPS_KAFKA_BROKERS"),
            False,
        ),
    ],
)
def test_an_outage_is_told_apart_from_an_event_the_broker_refused(exc, unavailable):
    import examlops.events as events

    assert events.is_unavailable(events.LogPublisher(), exc) is unavailable


def test_a_failure_with_no_message_is_still_a_reason(db):
    """`future.result(timeout)` raises a bare `TimeoutError` — which is exactly what an
    unanswering broker produces. Reported as `unavailable: ""`, it read as *no* reason: the
    control plane's check dropped it and `/health` stayed `ok` through a measured outage."""
    import examlops.events as events

    class _Silent:
        def publish(self, topic, payload, *, event_id):
            raise TimeoutError()  # no message, as `concurrent.futures` raises it

    assert events.describe(TimeoutError()) == "TimeoutError"
    assert events.describe(TimeoutError("nats: timeout")) == "TimeoutError: nats: timeout"

    events._publisher = _Silent()
    _queue(events, 1)
    result = events.relay_once()
    assert result["unavailable"] == "TimeoutError", "an outage with no message is still an outage"


def test_an_event_the_broker_refused_records_a_readable_reason(db):
    import examlops.events as events

    class _Rejects:
        def publish(self, topic, payload, *, event_id):
            raise ValueError()

    events._publisher = _Rejects()
    _queue(events, 1)
    events.relay_once()
    assert _last_error(db) == "ValueError"


def test_a_publisher_that_knows_its_own_transport_is_asked_first():
    import examlops.events as events

    class _Knows:
        def is_unavailable(self, exc):
            return "gone" in str(exc)

        def publish(self, topic, payload, *, event_id):
            raise AssertionError("not called")

    publisher = _Knows()
    # Its verdict wins in both directions, including against the generic signs.
    assert events.is_unavailable(publisher, RuntimeError("the broker is gone")) is True
    assert events.is_unavailable(publisher, ConnectionError("connection refused")) is False


def test_a_publisher_whose_own_verdict_raises_falls_back_to_the_signs():
    import examlops.events as events

    class _Raises:
        def is_unavailable(self, exc):
            raise RuntimeError("classification is broken")

        def publish(self, topic, payload, *, event_id):
            raise AssertionError("not called")

    assert events.is_unavailable(_Raises(), ConnectionError("connection refused")) is True


# ── the operator's own relay ──────────────────────────────────────────────────


def test_exa_events_relay_loop_stops_instead_of_spinning_on_a_dead_broker(db):
    """`--loop` drains until a pass claims nothing. A deferred batch *does* claim rows, so
    without stopping on the outage the command would re-defer the same backlog forever."""
    from typer.testing import CliRunner

    import examlops.events as events
    from examlops.cli.commands.events_cmd import app

    publisher = _Unreachable(limit=20)
    events._publisher = publisher
    _queue(events, 4)

    result = CliRunner().invoke(app, ["relay", "--loop"])
    assert result.exit_code == 0, result.output
    assert publisher.calls == 1, f"the command kept retrying ({publisher.calls} attempts)"
    assert "unavailable" in result.output.lower()
    assert db.outbox_stats() == {"pending": 4, "published": 0, "poison": 0}


def test_a_publisher_that_can_never_publish_defers_rather_than_poisons(db):
    """`kafka` is a skeleton that raises on every event. Spending the budget on it would turn a
    misconfiguration into permanent data loss instead of a visible, drainable backlog."""
    import examlops.events as events

    events._publisher = events.KafkaPublisher()
    _queue(events, 3)

    result = events.relay_once()
    assert result["deferred"] == 3 and result["failed"] == 0
    assert db.outbox_stats()["poison"] == 0
