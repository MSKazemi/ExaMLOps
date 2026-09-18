"""Event payloads have a published contract, and changes to it cannot break consumers (plan P2.6).

A consumer of ``retrain.run_completed`` breaks as surely as an HTTP client when a field it reads
disappears. `event-contract.json` is the committed snapshot of every topic's schema; these tests
fail on a change that could break a consumer, fail when the snapshot is merely stale, and fail
when code starts publishing a topic that has no schema at all.

Producer conformance is checked where the producers run: every control-plane test validates what
it enqueues (platform/services/control_plane/tests/conftest.py), and the serving and alias
producers are exercised below.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from examlops.events import envelope, schemas
from tests.unit._guard_deps import scan_files

REPO = Path(__file__).resolve().parents[2]


# ─── the snapshot: stale is a failure, breaking is a louder one ──────────────


def _types(schema: dict) -> set[str]:
    t = schema.get("type")
    return set() if t is None else {t} if isinstance(t, str) else set(t)


def _breaks(old: dict, new: dict, path: str) -> list[str]:
    """Why an event valid under ``new`` might be invalid under ``old`` — i.e. break a consumer."""
    problems: list[str] = []
    if _types(old) and not _types(new) <= _types(old):
        problems.append(f"{path}: type widened {sorted(_types(old))} → {sorted(_types(new))}")
    if "const" in old and new.get("const") != old["const"]:
        problems.append(f"{path}: const {old['const']!r} → {new.get('const')!r}")
    if "enum" in old and not set(new.get("enum", [None])) <= set(old["enum"]):
        problems.append(f"{path}: enum widened {old['enum']} → {new.get('enum')}")
    if "minimum" in old and new.get("minimum", float("-inf")) < old["minimum"]:
        problems.append(f"{path}: minimum lowered")
    if "maximum" in old and new.get("maximum", float("inf")) > old["maximum"]:
        problems.append(f"{path}: maximum raised")
    dropped = set(old.get("required", [])) - set(new.get("required", []))
    if dropped:
        problems.append(f"{path}: no longer required {sorted(dropped)}")
    new_props = new.get("properties", {})
    for key, sub in old.get("properties", {}).items():
        if key not in new_props:
            problems.append(f"{path}.{key}: removed")
        else:
            problems.extend(_breaks(sub, new_props[key], f"{path}.{key}"))
    old_extra, new_extra = old.get("additionalProperties"), new.get("additionalProperties")
    if isinstance(old_extra, dict):
        if not isinstance(new_extra, dict):
            problems.append(f"{path}: additionalProperties loosened")
        else:
            problems.extend(_breaks(old_extra, new_extra, f"{path}.*"))
    return problems


def _published() -> dict:
    return json.loads(schemas.CONTRACT_PATH.read_text(encoding="utf-8"))


def test_no_change_breaks_a_consumer_of_the_published_contract():
    published, current = _published(), schemas.contract()
    problems = [f"{t}: dropped" for t in published if t not in current]
    for topic, old in published.items():
        if topic in current:
            problems.extend(_breaks(old, current[topic], topic))
    assert not problems, (
        "these schema changes can break an existing consumer — publish a new topic version "
        "(<topic>.v2) alongside the old one instead of editing it in place:\n" + "\n".join(problems)
    )


def test_the_published_contract_is_current():
    assert schemas.contract() == _published(), (
        "event-contract.json is stale; after checking the change is additive, regenerate it with "
        "`python -m examlops.events.schemas --write` and commit the diff"
    )


@pytest.mark.parametrize(
    ("old", "new", "breaks"),
    [
        ({"type": "string"}, {"type": ["string", "null"]}, True),
        ({"type": ["string", "null"]}, {"type": "string"}, False),
        ({"enum": ["a"]}, {"enum": ["a", "b"]}, True),
        ({"required": ["x"], "properties": {"x": {}}}, {"properties": {"x": {}}}, True),
        ({"properties": {"x": {}}}, {"required": ["x"], "properties": {"x": {}}}, False),
        ({"properties": {"x": {}}}, {"properties": {}}, True),
        ({"properties": {}}, {"properties": {"y": {"type": "string"}}}, False),
        ({"maximum": 100}, {"maximum": 101}, True),
    ],
)
def test_the_compatibility_rule_itself(old, new, breaks):
    assert bool(_breaks(old, new, "t")) is breaks


# ─── the schemas are real JSON Schema, and the built-in validator agrees ─────


def test_every_schema_is_valid_draft_2020_12():
    jsonschema = pytest.importorskip("jsonschema")
    invalid = {}
    for topic, doc in schemas.contract().items():
        try:
            jsonschema.Draft202012Validator.check_schema(doc)
        except jsonschema.SchemaError as exc:
            invalid[topic] = exc.message
    assert not invalid, invalid


_SAMPLES = [
    ("model.alias_changed", {"model": "J"}),
    (
        "model.alias_changed",
        {
            "model": "J",
            "model_key": "j",
            "alias": "Production",
            "version": None,
            "previous_version": "3",
            "removed": True,
            "via": "dashboard",
        },
    ),
    ("serving.traffic_changed", {"model": "J", "model_key": "j", "rules": {"Production": 120}}),
    ("serving.traffic_changed", {"model": "J", "model_key": "j", "rules": {"Canary": 12.5}}),
    (
        "retrain.run_failed",
        {
            "command_key": "k",
            "flow_run_id": "f",
            "run_state": "COMPLETED",
            "model_name": None,
            "dataset_name": "d",
        },
    ),
    (
        "autopilot.cycle_complete",
        {"run_id": 1, "model_filter": None, "retrains": True, "promotions": 0, "dry_run": False},
    ),
    (
        "alert.drift",
        {
            "kind": "drift",
            "target": "J",
            "severity": "warn",
            "detail": "z=3",
            "value": 3.1,
            "threshold": 3,
        },
    ),
]


@pytest.mark.parametrize(("topic", "data"), _SAMPLES)
def test_the_builtin_validator_agrees_with_jsonschema(topic, data):
    jsonschema = pytest.importorskip("jsonschema")
    reference = not list(
        jsonschema.Draft202012Validator(schemas.schema_for(topic)).iter_errors(data)
    )
    assert (not schemas.validate(topic, data)) is reference


# ─── the envelope names its contract ─────────────────────────────────────────


def test_a_registered_topic_names_its_schema_in_the_envelope():
    event = envelope.build("retrain.scheduled", {}, event_id="x")
    assert event["dataschema"] == "urn:examlops:event-schema:retrain.scheduled"


def test_a_hand_published_topic_carries_no_schema():
    assert "dataschema" not in envelope.build("ops.note", {}, event_id="x")


# ─── producers publish only registered topics, with conforming payloads ──────

_LITERAL_TOPIC = re.compile(
    r"(?:enqueue_event|events\.publish|\bpublish)\(\s*\"([a-z_]+\.[a-z0-9_.]+)\""
    r"|event_topic=\"([a-z_]+\.[a-z0-9_.]+)\""
)
# Topics built at runtime, and every value they can take.
_TEMPLATED = {
    'f"{kind}.run_{run_state.lower()}"': {
        f"retrain.run_{s}" for s in ("completed", "failed", "cancelled", "crashed", "missing")
    },
    'f"alert.{kind}"': {"alert.cost", "alert.drift", "alert.retrain"},
}
_PRODUCER_ROOTS = (
    "platform/cli/src",
    "platform/services",
    "pipelines",
    "serving",
    "platform/clients",
)


def _producer_files():
    for root in _PRODUCER_ROOTS:
        for path in scan_files(REPO / root):
            posix = path.as_posix()
            if "/tests/" in posix or "node_modules" in posix:
                continue
            yield path


def test_every_published_topic_has_a_schema():
    unregistered: dict[str, str] = {}
    templated_seen: set[str] = set()
    for path in _producer_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if (
            "enqueue_event" not in text
            and "events.publish" not in text
            and "event_topic" not in text
        ):
            continue
        for match in _LITERAL_TOPIC.finditer(text):
            topic = match.group(1) or match.group(2)
            if topic not in schemas.SCHEMAS:
                unregistered[topic] = path.relative_to(REPO).as_posix()
        for template in _TEMPLATED:
            if template in text:
                templated_seen.add(template)
    # The dashboard's in-process realtime bus (`bus.publish`) is not the backbone.
    unregistered = {t: f for t, f in unregistered.items() if "dashboard" not in f}
    assert not unregistered, f"topics published without a schema: {unregistered}"
    assert templated_seen == set(_TEMPLATED), "a templated topic moved; update _TEMPLATED"
    for values in _TEMPLATED.values():
        assert values <= set(schemas.SCHEMAS)


def test_serving_and_alias_producers_conform(monkeypatch):
    from examlops import events
    from examlops.data import serving
    from examlops.platform_db import get_db, init_db

    serving.set_traffic_rules("JPCP", {"Production": 90, "Canary": 10}, updated_by="a")
    serving.set_shadow_config("JPCP", enabled=True, updated_by="a")
    serving.set_shadow_config("JPCP", enabled=False, updated_by="a")
    events.alias_changed("JPCP", "Production", 7, previous_version=6)
    events.alias_changed("JPCP", "Canary", None, removed=True)

    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT topic, payload FROM event_outbox").fetchall()
    assert len(rows) == 5
    for topic, payload in rows:
        assert schemas.validate(topic, json.loads(payload)) == [], topic
