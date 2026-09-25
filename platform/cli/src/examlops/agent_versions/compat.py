"""The state-compatibility gate (ADR 0146 decision 5): compatible, incompatible or inert.

An agent's checkpoint is data written by one version and read by the next. LangGraph's own
guidance is the whole rule set: adding a field with a default is safe; renaming or removing a
field loses data; an interrupted thread cannot survive the node it is parked on being renamed
or removed. No surveyed platform migrates checkpoints automatically, and neither does this one.

A manifest declares its checkpoint schema under ``state.schema``::

    state:
      schema_version: 3
      schema_hash: sha256:...            # = state_schema_hash(schema), checked at registration
      schema:
        fields: {messages: {type: list}, step: {type: int, default: 0}}
        nodes: [plan, act, review]
        interrupt_nodes: [review]        # optional: nodes a thread may be parked on

The diff has three outcomes:

* ``compatible`` - only additive fields that carry a default; no field, type or node removed.
* ``incompatible`` - anything else. Promotion needs a declared strategy: ``pin`` (in-flight
  threads stay on the old version until they close) or ``drain`` (in-flight work finishes on the
  old version; the thread's next input starts on the new one). Without one: blocked.
* ``inert`` - either side has no exported schema. Treated as incompatible and **never** reported
  compatible: an unknown schema is not evidence of a safe one.

Pure - no database, no network.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "COMPATIBLE",
    "INCOMPATIBLE",
    "INERT",
    "STATE_STRATEGIES",
    "check_state_schema",
    "gate_state",
    "state_compat",
    "state_schema_hash",
]

COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"
INERT = "inert"
#: The two declared strategies an incompatible (or inert) change may promote with.
STATE_STRATEGIES = ("pin", "drain")

_SCHEMA_KEYS = {"fields", "nodes", "interrupt_nodes"}
_FIELD_KEYS = {"type", "default"}


def state_schema_hash(schema: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of a ``state.schema`` object."""
    blob = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check_state_schema(schema: Any, where: str = "state.schema") -> list[str]:
    """Problems with a ``state.schema`` object (empty = valid)."""
    if not isinstance(schema, dict):
        return [f"{where}: must be an object"]
    out: list[str] = []
    for k in sorted(set(schema) - _SCHEMA_KEYS):
        out.append(f"{where}.{k}: unknown field")
    fields = schema.get("fields")
    if not isinstance(fields, dict) or not fields:
        out.append(f"{where}.fields: required non-empty object of name -> {{type, default?}}")
    else:
        for name, spec in fields.items():
            if not isinstance(spec, dict):
                out.append(f"{where}.fields.{name}: must be an object")
                continue
            for k in sorted(set(spec) - _FIELD_KEYS):
                out.append(f"{where}.fields.{name}.{k}: unknown field")
            if not isinstance(spec.get("type"), str) or not spec["type"].strip():
                out.append(f"{where}.fields.{name}.type: required non-empty string")
    nodes = schema.get("nodes")
    if not isinstance(nodes, list) or not nodes or not all(isinstance(n, str) for n in nodes):
        out.append(f"{where}.nodes: required non-empty list of node names")
        nodes = []
    elif len(set(nodes)) != len(nodes):
        out.append(f"{where}.nodes: a node is listed twice")
    inter = schema.get("interrupt_nodes")
    if inter is not None:
        if not isinstance(inter, list) or not all(isinstance(n, str) for n in inter):
            out.append(f"{where}.interrupt_nodes: must be a list of node names")
        else:
            for n in inter:
                if n not in nodes:
                    out.append(f"{where}.interrupt_nodes: {n!r} is not one of the nodes")
    return out


def _schema(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    schema = state.get("schema")
    return schema if isinstance(schema, dict) and not check_state_schema(schema) else None


def state_compat(
    running: dict[str, Any] | None, candidate: dict[str, Any] | None
) -> dict[str, Any]:
    """Diff two manifests' ``state`` sections. Returns ``{outcome, changes, reasons}``.

    ``changes`` lists every difference found; ``reasons`` only the ones that make the change
    incompatible (or the reason it is inert).
    """
    old, new = _schema(running), _schema(candidate)
    if old is None or new is None:
        side = "running" if old is None else "candidate"
        if old is None and new is None:
            side = "running and candidate"
        return {
            "outcome": INERT,
            "changes": [],
            "reasons": [f"no exported state schema on the {side} version (inert)"],
        }
    changes: list[dict[str, Any]] = []
    reasons: list[str] = []
    of, nf = old["fields"], new["fields"]
    for name in sorted(set(of) - set(nf)):
        changes.append({"kind": "field_removed", "name": name})
        reasons.append(f"field {name!r} removed or renamed (checkpointed data would be lost)")
    for name in sorted(set(nf) - set(of)):
        if "default" in nf[name]:
            changes.append({"kind": "field_added", "name": name, "default": True})
        else:
            changes.append({"kind": "field_added", "name": name, "default": False})
            reasons.append(f"field {name!r} added without a default (old checkpoints lack it)")
    for name in sorted(set(of) & set(nf)):
        if of[name].get("type") != nf[name].get("type"):
            changes.append(
                {
                    "kind": "field_type_changed",
                    "name": name,
                    "from": of[name].get("type"),
                    "to": nf[name].get("type"),
                }
            )
            reasons.append(
                f"field {name!r} changed type {of[name].get('type')} -> {nf[name].get('type')}"
            )
    parked = set(old.get("interrupt_nodes") or old["nodes"])
    for node in [n for n in old["nodes"] if n not in set(new["nodes"])]:
        where = " (an interrupted thread may be parked on it)" if node in parked else ""
        changes.append({"kind": "node_removed", "name": node, "interrupt_reachable": bool(where)})
        reasons.append(f"node {node!r} removed or renamed{where}")
    for node in [n for n in new["nodes"] if n not in set(old["nodes"])]:
        changes.append({"kind": "node_added", "name": node})
    return {
        "outcome": INCOMPATIBLE if reasons else COMPATIBLE,
        "changes": changes,
        "reasons": reasons,
    }


def gate_state(result: dict[str, Any], strategy: str | None) -> list[str]:
    """Refusal reasons for a compat ``result`` promoted with ``strategy`` (empty = passes)."""
    if strategy is not None and strategy not in STATE_STRATEGIES:
        return [f"state strategy {strategy!r} is not one of {', '.join(STATE_STRATEGIES)}"]
    if result["outcome"] == COMPATIBLE:
        return []
    if strategy in STATE_STRATEGIES:
        return []
    return [
        f"state change is {result['outcome']}: "
        + "; ".join(result["reasons"])
        + " - declare --state-strategy pin or drain, or keep the schema additive"
    ]
