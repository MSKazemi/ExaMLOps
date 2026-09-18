"""A retrained model is considered for promotion when its run finishes (ADR 0085 × ADR 0124).

The autopilot's cycle ends when it dispatches a retrain, so the candidate it trains used to wait
for the *next* scheduled cycle. ``exa autopilot follow`` consumes ``retrain.run_completed`` and
runs that model's cycle immediately — the same ``run_cycle``, so every gate still applies. These
tests pin when it acts, when it stays quiet, and that a busy lease means "later", not "never".
"""

from __future__ import annotations

import pytest

from examlops.cli.commands import autopilot_cmd as ap
from examlops.data import get_db, init_db
from examlops.events import envelope
from examlops.events.consumer import EventConsumer


def _event(model: str | None = "JPCP", event_id: str = "outbox:5") -> dict:
    data = {"command_key": "k", "flow_run_id": "f", "run_state": "COMPLETED"}
    if model is not None:
        data["model_name"] = model
    return envelope.build("retrain.run_completed", data, event_id=event_id)


def _rule(model: str, enabled: int = 1) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO promotion_rules (model, metric, operator, threshold, from_alias, "
            "to_alias, enabled) VALUES (?, 'rmse', 'lt', 5.0, 'Staging', 'Production', ?)",
            (model, enabled),
        )


@pytest.fixture()
def cycles(monkeypatch):
    calls: list[dict] = []
    outcome: dict = {}

    def fake_cycle(**kwargs):
        calls.append(kwargs)
        return dict(outcome)

    monkeypatch.setattr(ap, "run_cycle", fake_cycle)
    monkeypatch.setattr(ap, "_is_enabled", lambda: True)
    fake_cycle.outcome = outcome  # type: ignore[attr-defined]
    return calls, outcome


def test_a_completed_run_triggers_that_models_cycle(cycles):
    calls, _ = cycles
    _rule("jpcp")

    assert ap.on_run_completed(_event("JPCP", "outbox:42")) == "cycle_ran"
    assert calls == [{"model_filter": "JPCP", "triggered_by": "event:outbox:42"}]


def test_nothing_happens_while_the_kill_switch_is_off(cycles, monkeypatch):
    calls, _ = cycles
    _rule("jpcp")
    monkeypatch.setattr(ap, "_is_enabled", lambda: False)

    assert ap.on_run_completed(_event()) == "disabled"
    assert calls == []


@pytest.mark.parametrize("enabled", [0, None])
def test_a_model_without_an_enabled_promotion_rule_is_left_to_the_schedule(cycles, enabled):
    calls, _ = cycles
    if enabled is not None:
        _rule("jpcp", enabled=enabled)

    assert ap.on_run_completed(_event()) == "no_rule"
    assert calls == []


def test_an_event_without_a_model_is_ignored(cycles):
    calls, _ = cycles
    assert ap.on_run_completed(_event(model=None)) == "ignored"
    assert calls == []


def test_a_busy_lease_asks_for_redelivery_instead_of_dropping_the_event(cycles):
    _, outcome = cycles
    _rule("jpcp")
    outcome.update({"enabled": True, "skipped": True, "reason": "lease held"})

    with pytest.raises(ap.CycleBusy):
        ap.on_run_completed(_event())


class _Msg:
    def __init__(self, body: bytes) -> None:
        self.data = body
        self.metadata = type("M", (), {"num_delivered": 1})()
        self.settled: list[str] = []

    async def ack(self):
        self.settled.append("ack")

    async def nak(self, delay=None):
        self.settled.append("nak")


class _Stream:
    prefix = "examlops.events"

    def settle(self, coro):
        import asyncio

        return asyncio.run(coro)


def test_through_the_consumer_a_busy_lease_is_retried_and_a_run_is_handled_once(cycles):
    calls, outcome = cycles
    _rule("jpcp")
    consumer = EventConsumer("autopilot-test", ap.on_run_completed, stream=_Stream())

    outcome.update({"skipped": True})
    busy = _Msg(envelope.encode(_event(event_id="outbox:7")))
    assert consumer.handle(busy) == "retry" and busy.settled == ["nak"]

    outcome.clear()
    assert consumer.handle(_Msg(envelope.encode(_event(event_id="outbox:7")))) == "handled"
    assert consumer.handle(_Msg(envelope.encode(_event(event_id="outbox:7")))) == "duplicate"
    assert len(calls) == 2  # the busy attempt and the one that ran; the duplicate did nothing


def test_follow_refuses_to_start_without_the_backbone(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.delenv("EXAMLOPS_NATS_URL", raising=False)
    result = CliRunner().invoke(app, ["autopilot", "follow"])
    assert result.exit_code != 0
    assert "EXAMLOPS_NATS_URL" in result.output
