"""A drift status change is announced once, and every consumer agrees on the status (plan P2.4b).

``examlops.drift_status`` is the one computation behind ``exa drift status``, ``exa drift
trigger``, the autopilot and the control plane's evaluator. ``evaluate`` records each model's
status and emits ``drift.status_changed`` (plus ``alert.drift`` for WARNING/CRITICAL) only when it
changes, in the same transaction as the record.
"""

from __future__ import annotations

import json
import threading

import pytest

from examlops import drift_status
from examlops.data.drift import set_drift_baseline, write_drift_snapshot
from examlops.platform_db import get_db, init_db


def _predict(model: str, values: list[float]) -> None:
    for v in values:
        write_drift_snapshot(model, "Production", v, None)


def _events(topic: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_outbox WHERE topic=? ORDER BY id", (topic,)
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


@pytest.fixture()
def model():
    init_db()
    set_drift_baseline("jpcp", {"mean": 10.0, "std": 1.0, "n": 500.0})
    return "jpcp"


def test_a_first_look_at_a_healthy_model_is_recorded_quietly(model):
    _predict(model, [10.0] * 20)
    assert drift_status.evaluate() == []
    assert _events("drift.status_changed") == []
    with get_db() as conn:
        assert conn.execute("SELECT status FROM drift_status_state").fetchone()["status"] == "OK"


def test_a_change_is_announced_once_with_what_it_left(model):
    _predict(model, [10.0] * 20)
    drift_status.evaluate()
    _predict(model, [14.0] * 100)  # the whole window moves to z=4
    [change] = drift_status.evaluate()
    assert (change["previous"], change["status"]) == ("OK", "CRITICAL")
    assert change["z_score"] == pytest.approx(4.0)
    assert drift_status.evaluate() == []  # still CRITICAL: nothing new to say
    assert [e["status"] for e in _events("drift.status_changed")] == ["CRITICAL"]
    [alert] = _events("alert.drift")
    assert alert["severity"] == "critical" and alert["target"] == model
    assert "OK → CRITICAL" in alert["detail"]


def test_a_recovery_is_announced_but_raises_no_alert(model):
    _predict(model, [14.0] * 100)
    drift_status.evaluate()
    _predict(model, [10.0] * 100)
    [change] = drift_status.evaluate()
    assert (change["previous"], change["status"]) == ("CRITICAL", "OK")
    assert len(_events("alert.drift")) == 1  # only the escalation


def test_a_model_first_seen_already_drifting_is_announced(model):
    _predict(model, [12.5] * 50)  # z=2.5
    [change] = drift_status.evaluate()
    assert change["previous"] is None and change["status"] == "WARNING"
    assert _events("alert.drift")[0]["severity"] == "warn"


def test_two_evaluators_at_once_announce_a_change_once(model):
    """Two control-plane replicas evaluating together: the check runs under the write lock."""
    _predict(model, [10.0] * 20)
    drift_status.evaluate()
    _predict(model, [14.0] * 100)
    rows = drift_status.model_rows()
    barrier = threading.Barrier(4)
    results: list[list] = []

    def evaluator() -> None:
        barrier.wait(5)
        results.append(drift_status.record_transitions(rows))

    threads = [threading.Thread(target=evaluator) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sum(len(r) for r in results) == 1
    assert len(_events("drift.status_changed")) == 1


def test_the_events_match_their_published_schema(model):
    from examlops.events import schemas

    _predict(model, [14.0] * 100)
    drift_status.evaluate()
    for topic in ("drift.status_changed", "alert.drift"):
        for payload in _events(topic):
            assert schemas.validate(topic, payload) == [], (topic, payload)


def test_the_configured_drift_provider_decides_for_every_consumer(model, monkeypatch):
    """The autopilot used its own z-score with fixed thresholds and ignored the provider."""
    _predict(model, [10.0] * 20)
    monkeypatch.setattr(
        drift_status, "resolve_drift_score_fn", lambda: lambda *_: (9.9, "CRITICAL")
    )
    assert drift_status.model_row(model)["status"] == "CRITICAL"
    from examlops.cli.commands import autopilot_cmd, drift

    assert drift._drift_rows(model)[0]["status"] == "CRITICAL"
    # The autopilot has no private copy of the computation any more.
    assert not hasattr(autopilot_cmd, "_compute_z")
    assert "drift_status.model_row" in open(autopilot_cmd.__file__, encoding="utf-8").read()
