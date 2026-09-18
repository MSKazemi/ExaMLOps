"""The audit log streams to the event backbone, verifiably (plan P2.4b).

With ``EXAMLOPS_AUDIT_STREAM`` on, every audit append also enqueues ``audit.recorded`` in the same
transaction, carrying the fields the hash chain covers. A receiver (a SIEM) can then check the
chain itself with ``verify_audit_stream``: an edited event breaks its own hash, a deleted or
reordered one breaks the link to its neighbour.
"""

from __future__ import annotations

import json

import pytest

from examlops.data.audit import verify_audit_stream, write_audit_event
from examlops.events import schemas
from examlops.platform_db import get_db, init_db


def _streamed() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_outbox WHERE topic='audit.recorded' ORDER BY id"
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


@pytest.fixture()
def stream(monkeypatch):
    init_db()
    monkeypatch.setenv("EXAMLOPS_AUDIT_STREAM", "1")


def test_off_by_default(monkeypatch):
    init_db()
    monkeypatch.delenv("EXAMLOPS_AUDIT_STREAM", raising=False)
    write_audit_event("cli", "alice", "model_promoted", "jpcp", {"version": 3})
    assert _streamed() == []


def test_each_audit_event_is_streamed_with_its_chain(stream):
    write_audit_event("cli", "alice", "model_promoted", "jpcp", {"version": 3})
    write_audit_event("dashboard", None, "traffic_changed", "jpcp")
    events = _streamed()
    assert [e["action"] for e in events] == ["model_promoted", "traffic_changed"]
    with get_db() as conn:
        stored = conn.execute("SELECT id, hash FROM audit_events ORDER BY id").fetchall()
    assert [(e["id"], e["hash"]) for e in events] == [(r["id"], r["hash"]) for r in stored]
    assert events[1]["prev_hash"] == events[0]["hash"]
    for event in events:
        assert schemas.validate("audit.recorded", event) == [], event


def test_a_receiver_can_verify_the_run(stream):
    for i in range(4):
        write_audit_event("cli", "alice", "step", f"t{i}", {"i": i})
    events = _streamed()
    assert verify_audit_stream(events) == []

    edited = [dict(e) for e in events]
    edited[1]["target"] = "someone-else"
    assert verify_audit_stream(edited) == [
        f"event {events[1]['id']}: its hash does not match its contents"
    ]

    missing = events[:1] + events[2:]
    assert verify_audit_stream(missing) == [
        f"event {events[2]['id']}: does not follow the event before it"
    ]


def test_the_event_commits_with_the_audit_row_or_not_at_all(stream):
    from examlops.data.audit import append_audit_event

    with pytest.raises(RuntimeError):
        with get_db() as conn:
            append_audit_event(conn, "cli", "alice", "rolled_back", "jpcp")
            raise RuntimeError("the caller's change failed")
    with get_db() as conn:
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
    assert _streamed() == []


def test_every_service_that_writes_audit_events_can_be_switched_on():
    """A service that neither loads .env nor passes the variable would stream nothing."""
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "platform/infra/docker-compose/docker-compose.yml"
        ).read_text()
    )
    writers = [
        "control-plane", "dashboard", "agent", "seanerbus-bridge", "autopilot-follower",
        "skipper-watch", "dataplane", "ray-serving", "gateway-authz", "backup",
    ]  # fmt: skip
    missing = []
    for name in writers:
        service = compose["services"][name]
        files = [f["path"] if isinstance(f, dict) else f for f in service.get("env_file") or []]
        if ".env" not in files and "EXAMLOPS_AUDIT_STREAM" not in (
            service.get("environment") or {}
        ):
            missing.append(name)
    assert not missing, missing
