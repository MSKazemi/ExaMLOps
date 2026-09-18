"""skipper-watch alerts on a failed training run as it happens, once per run (ADR 0104 × ADR 0124).

Drift and cost are measurements and stay polled. A run that fails is an event: the watch consumes
``retrain.*`` from the backbone and raises ``alert.retrain`` through its usual fan-out (outbox,
audit, episodic memory). The consumer's inbox makes a redelivered event a no-op, so one failed run
is one alert.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import watch  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "watch-events.db"))
    from examlops.data import init_db

    init_db()
    yield


def _run_event(state: str, model: str = "JPCP", event_id: str = "outbox:3") -> dict:
    from examlops.events import envelope

    return envelope.build(
        f"retrain.run_{state.lower()}",
        {
            "command_key": "k",
            "flow_run_id": "55bec2da-0000-4000-8000-000000000000",
            "run_state": state,
            "model_name": model,
            "dataset_name": "PM100Dataset",
        },
        event_id=event_id,
    )


@pytest.mark.parametrize(
    ("state", "severity"), [("FAILED", "critical"), ("CRASHED", "critical"), ("MISSING", "warn")]
)
def test_a_run_that_did_not_finish_is_an_alert(state, severity):
    alert = watch.alert_for_event(_run_event(state))
    assert alert["kind"] == "retrain" and alert["target"] == "JPCP"
    assert alert["severity"] == severity
    assert "55bec2da" in alert["detail"]


@pytest.mark.parametrize("state", ["COMPLETED", "CANCELLED"])
def test_a_run_that_finished_or_was_cancelled_is_not(state):
    assert watch.alert_for_event(_run_event(state)) is None


def test_the_alert_fans_out_like_every_watch_alert(db):
    from examlops.data import get_db
    from examlops.events import schemas

    watch.on_event(_run_event("FAILED"))

    with get_db() as conn:
        payloads = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM event_outbox WHERE topic='alert.retrain'"
            ).fetchall()
        ]
        audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE source='skipper-watch' AND action='alert_raised'"
        ).fetchone()[0]
    import json

    assert len(payloads) == 1 and audits == 1
    assert schemas.validate("alert.retrain", json.loads(payloads[0])) == []


class _Msg:
    def __init__(self, body: bytes) -> None:
        self.data = body
        self.metadata = type("M", (), {"num_delivered": 1})()

    async def ack(self):
        pass


class _Stream:
    prefix = "examlops.events"

    def settle(self, coro):
        import asyncio

        return asyncio.run(coro)


def test_one_failed_run_is_one_alert_however_often_it_is_delivered(db):
    from examlops.data import get_db
    from examlops.events import envelope
    from examlops.events.consumer import EventConsumer

    consumer = EventConsumer("skipper-watch-test", watch.on_event, stream=_Stream())
    body = envelope.encode(_run_event("FAILED", event_id="outbox:77"))

    assert consumer.handle(_Msg(body)) == "handled"
    assert consumer.handle(_Msg(body)) == "duplicate"

    with get_db() as conn:
        raised = conn.execute(
            "SELECT count(*) FROM event_outbox WHERE topic='alert.retrain'"
        ).fetchone()[0]
    assert raised == 1


def test_follow_needs_the_backbone(monkeypatch, capsys):
    monkeypatch.delenv("EXAMLOPS_NATS_URL", raising=False)
    assert watch._main(["--follow"]) == 2
    assert "EXAMLOPS_NATS_URL" in capsys.readouterr().out
