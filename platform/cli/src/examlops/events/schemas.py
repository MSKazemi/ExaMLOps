"""JSON Schemas for the ``data`` of every event the platform publishes (plan P2.6, ADR 0124).

A consumer written against ``retrain.run_completed`` depends on ``run_state`` being there and being
a string, exactly as a client of ``/v1/retrain`` depends on the response shape. The HTTP API has a
committed contract (``api-contract.json``); these schemas are the events' equivalent:

* every envelope of a registered topic carries ``dataschema`` = :func:`dataschema_uri`, so a
  consumer can tell which contract an event was produced under;
* ``event-contract.json`` next to this file is the published snapshot, and
  ``tests/unit/test_event_schemas.py`` fails on any change that could break a consumer. The rule
  is that every event valid under the new schema must be valid under the old one: a dropped topic,
  a field removed or no longer required, a widened type (``string`` → ``string | null``), a new
  enum value or a looser bound all break it. Adding an optional field, adding a topic or
  tightening a rule does not, and only needs the snapshot regenerated:
  ``python -m examlops.events.schemas --write``;
* a breaking change is a new topic version (``<topic>.v2``) published alongside the old one for as
  long as consumers need, never an edit in place.

Schemas are deliberately permissive about *extra* fields (``additionalProperties`` is left open):
producers may add, consumers must ignore what they do not know. Topics that are not registered —
``exa events publish`` lets an operator hand-publish anything — carry no ``dataschema``.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parent / "event-contract.json"
_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

# Present on control-plane events: the same principal and tenant the envelope carries as the
# `examlopsactor` / `examlopstenant` extensions, kept in `data` for consumers that read only data.
_WHO = {"actor": _OPT_STR, "tenant": _OPT_STR}

_RUN_STATES = ["COMPLETED", "FAILED", "CANCELLED", "CRASHED", "MISSING"]


def _object(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**required, **(optional or {})},
        "required": sorted(required),
    }


def _run_schema(state: str) -> dict[str, Any]:
    return _object(
        {
            "command_key": _STR,
            "flow_run_id": _STR,
            "run_state": {"type": "string", "const": state},
            "model_name": _OPT_STR,
            "dataset_name": _OPT_STR,
        },
        _WHO,
    )


_ALERT = _object(
    {
        "kind": _STR,
        "target": _STR,
        "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
        "detail": _STR,
    },
    {"value": {"type": ["number", "null"]}, "threshold": {"type": ["number", "null"]}},
)

SCHEMAS: dict[str, dict[str, Any]] = {
    "retrain.scheduled": _object(
        {"model_name": _OPT_STR, "dataset_name": _OPT_STR, "flow_run_id": _STR},
        {"command_key": _STR, **_WHO},
    ),
    **{f"retrain.run_{s.lower()}": _run_schema(s) for s in _RUN_STATES},
    "approval.approved": _object(
        {"approval_id": _STR, "model_id": _STR, "flow_run_id": _STR},
        {"command_key": _STR, **_WHO},
    ),
    "approval.rejected": _object({"approval_id": _STR, "model_id": _STR, "reason": _OPT_STR}, _WHO),
    "approval.retracted": _object({"approval_id": _STR, "model_id": _STR}, _WHO),
    "modelzoo.retrain_scheduled": _object(
        {"model_id": _STR, "commit_sha": _STR, "flow_run_id": _STR},
        {"command_key": _STR, **_WHO},
    ),
    "serving.traffic_changed": _object(
        {
            "model": _STR,
            "model_key": _STR,
            "rules": {
                "type": "object",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100},
            },
        }
    ),
    "serving.shadow_changed": _object(
        {"model": _STR, "model_key": _STR, "enabled": _BOOL, "shadow_alias": _STR}
    ),
    "serving.snapshot_published": _object(
        {"generation": _INT, "digest": _STR, "models": _INT},
    ),
    "model.alias_changed": _object(
        {
            "model": _STR,
            "model_key": _STR,
            "alias": _STR,
            "version": _OPT_STR,
            "previous_version": _OPT_STR,
            "removed": _BOOL,
            "via": _STR,
        }
    ),
    "autopilot.cycle_complete": _object(
        {
            "run_id": {"type": ["integer", "string"]},
            "model_filter": _OPT_STR,
            "retrains": _INT,
            "promotions": _INT,
            "dry_run": _BOOL,
        }
    ),
    # A model's prediction-drift status changed (plan P2.4b): emitted once per change by
    # examlops.drift_status, with the status it left (null the first time it is seen, already bad).
    "drift.status_changed": _object(
        {"model": _STR, "status": _STR, "z_score": {"type": "number"}},
        {
            "previous": _OPT_STR,
            "live_mean": {"type": "number"},
            "baseline_mean": {"type": ["number", "null"]},
            "n_snapshots": _INT,
        },
    ),
    # Every audit event, when EXAMLOPS_AUDIT_STREAM is on (plan P2.4b): the fields the hash
    # chain covers, exactly as stored, so a receiver can recompute and chain them
    # (examlops.data.audit.verify_audit_stream).
    "audit.recorded": _object(
        {
            "id": _INT,
            "ts": _STR,
            "source": _STR,
            "action": _STR,
            "tenant": _STR,
            "prev_hash": _STR,
            "hash": _STR,
        },
        {
            "actor": _OPT_STR,
            "target": _OPT_STR,
            "details": _OPT_STR,
            "correlation": {"type": "object"},
        },
    ),
    "alert.cost": _ALERT,
    "alert.drift": _ALERT,
    "alert.retrain": _ALERT,
}


def schema_for(topic: str) -> dict[str, Any] | None:
    """The full JSON Schema document for ``topic``'s data, or ``None`` if it is unregistered."""
    body = SCHEMAS.get(topic)
    if body is None:
        return None
    return {
        "$schema": _DRAFT,
        "$id": dataschema_uri(topic),
        "title": topic,
        **copy.deepcopy(body),
    }


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected: str | list[str]) -> bool:
    for name in [expected] if isinstance(expected, str) else expected:
        # bool is an int in Python and not an integer in JSON Schema.
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, _JSON_TYPES[name]):
            return True
    return False


def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "type" in schema and not _type_ok(value, schema["type"]):
        errors.append(f"{path}: expected {schema['type']}, got {type(value).__name__}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                _check(item, props[key], f"{path}.{key}", errors)
            elif isinstance(extra, dict):
                _check(item, extra, f"{path}.{key}", errors)


def validate(topic: str, data: Any) -> list[str]:
    """Violations of ``topic``'s schema by ``data`` (empty = valid, or topic unregistered).

    Dependency-free, and covers exactly the keywords these schemas use (type, const, enum,
    required, properties, additionalProperties, minimum, maximum); the unit tests cross-check it
    against the `jsonschema` library where that is installed.
    """
    schema = SCHEMAS.get(topic)
    if schema is None:
        return []
    errors: list[str] = []
    _check(data, schema, "data", errors)
    return errors


def dataschema_uri(topic: str) -> str:
    """The CloudEvents ``dataschema`` for a registered topic (versioned by topic name)."""
    return f"urn:examlops:event-schema:{topic}"


def contract() -> dict[str, Any]:
    """Every registered schema, keyed by topic — what ``event-contract.json`` holds."""
    return {topic: schema_for(topic) for topic in sorted(SCHEMAS)}


def write_contract(path: Path = CONTRACT_PATH) -> Path:
    path.write_text(json.dumps(contract(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":  # python -m examlops.events.schemas --write
    if "--write" in sys.argv[1:]:
        print(f"wrote {write_contract()}")
    else:
        print(json.dumps(contract(), indent=2, sort_keys=True))
