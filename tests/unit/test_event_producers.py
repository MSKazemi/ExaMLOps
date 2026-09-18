"""Serving-config and alias changes are published, not just written (P2.4, ADR 0124).

Until P2.4 a traffic split, a shadow target or a promotion changed the database (or MLflow) and
told nobody: the serving plane found out by polling. These tests pin that each change enqueues its
event — atomically with the write where the platform database is the system of record — and a
static guard fails when a new surface moves an MLflow alias without announcing it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from examlops import events
from examlops.data import events as data_events
from examlops.data import serving
from examlops.platform_db import get_db, init_db
from tests.unit._guard_deps import scan_files

REPO = Path(__file__).resolve().parents[2]


def _outbox(topic: str) -> list[dict]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT topic, payload, actor, tenant FROM event_outbox WHERE topic=? ORDER BY id",
            (topic,),
        ).fetchall()
    return [{"topic": r[0], "data": json.loads(r[1]), "actor": r[2], "tenant": r[3]} for r in rows]


# ─── serving config: same transaction as the write ───────────────────────────


def test_a_traffic_split_publishes_serving_traffic_changed():
    serving.set_traffic_rules("JPCP", {"Production": 90, "Canary": 10}, updated_by="alice")

    [event] = _outbox("serving.traffic_changed")
    assert event["data"] == {
        "model": "JPCP",
        "model_key": "jpcp",
        "rules": {"Production": 90, "Canary": 10},
    }
    assert event["actor"] == "alice"


def test_a_traffic_split_and_its_event_commit_together(monkeypatch):
    """No event without the change — and no change without the event."""

    def refuse(*_a, **_k):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(serving, "enqueue_event", refuse)

    with pytest.raises(RuntimeError):
        serving.set_traffic_rules("JPCP", {"Production": 50, "Canary": 50}, updated_by="alice")

    assert serving.get_traffic_rules("JPCP") is None


@pytest.mark.parametrize("enabled", [True, False])
def test_a_shadow_change_publishes_serving_shadow_changed(enabled):
    serving.set_shadow_config("MACK", shadow_alias="Staging", enabled=True, updated_by="bob")
    if not enabled:
        serving.set_shadow_config("MACK", enabled=False, updated_by="bob")

    events_ = _outbox("serving.shadow_changed")
    assert events_[-1]["data"]["enabled"] is enabled
    assert events_[-1]["data"]["model_key"] == "mack"
    assert events_[-1]["actor"] == "bob"


def test_actor_and_tenant_default_when_unset():
    data_events.enqueue_event("t.defaults", {})
    [event] = _outbox("t.defaults")
    assert (event["actor"], event["tenant"]) == ("system", "default")


# ─── MLflow aliases: announced after MLflow accepts the change ───────────────


def test_alias_changed_enqueues_the_move():
    outbox_id = events.alias_changed(
        "JPCP", "Production", 7, previous_version=6, actor="carol", via="exa-rollback"
    )

    assert outbox_id
    [event] = _outbox("model.alias_changed")
    assert event["data"] == {
        "model": "JPCP",
        "model_key": "jpcp",
        "alias": "Production",
        "version": "7",
        "previous_version": "6",
        "removed": False,
        "via": "exa-rollback",
    }
    assert event["actor"] == "carol"


def test_a_removed_alias_has_no_version():
    events.alias_changed("JPCP", "Canary", None, previous_version="4", removed=True)
    [event] = _outbox("model.alias_changed")
    assert event["data"]["removed"] is True and event["data"]["version"] is None


def test_a_lost_alias_event_never_fails_the_promotion(monkeypatch, caplog):
    """MLflow already moved the alias; the serving plane's alias poll is the backstop."""

    def refuse(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(events, "publish", refuse)

    with caplog.at_level(logging.WARNING, logger="examlops.events"):
        assert events.alias_changed("JPCP", "Production", 7) is None

    assert "not enqueued" in caplog.text


# ─── guard: every surface that moves an alias announces it ──────────────────

_SCANNED = ("platform/cli/src", "pipelines", "serving", "platform/services", "platform/clients")


def _alias_writers() -> list[Path]:
    found = []
    for root in _SCANNED:
        for path in scan_files(REPO / root):
            if "/tests/" in path.as_posix() or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "registered_model_alias(" in text or "registered-models/alias" in text:
                found.append(path)
    return found


def test_the_guard_sees_the_known_alias_writers():
    names = {p.name for p in _alias_writers()}
    assert {"pipeline_generator.py", "rollback_cmd.py", "models.py", "platform_ops.py"} <= names


def test_every_surface_that_moves_an_alias_announces_it():
    silent = []
    for path in _alias_writers():
        text = path.read_text(encoding="utf-8", errors="replace")
        writes = "set_registered_model_alias(" in text or re.search(
            r"(?:POST|\"DELETE\"|\.post\()[\s\S]{0,200}registered-models/alias", text
        )
        if writes and not re.search(r"(?<![\w])(?:alias_changed|_announce_alias)\(", text):
            silent.append(path.relative_to(REPO).as_posix())
    assert not silent, (
        "these files move an MLflow alias without publishing model.alias_changed — call "
        f"examlops.events.alias_changed after the write: {silent}"
    )
