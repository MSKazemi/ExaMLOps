"""The registry YAML's ``placement:`` section — where and on what a training run asks to land.

ADR 0080 decision 2 makes the per-model registry YAML *the* IR and says to extend it "as needed".
Before this section the YAML had no field for a training step's resource ask or for the target
cluster, so lowering a pipeline dropped both and ``run --ir`` smuggled them through as out-of-band
hints. With it, the YAML carries everything a ``training`` pipeline states, and an operator who
authors YAML directly gets the same run-time behaviour as one who authors the DSL::

    placement:
      cluster: auto        # an ACTIVE cluster name, or 'auto' for placement (ADR 0077)
      gpus: 2              # the train step's scheduler-neutral ask (hpc_placement.ResourceAsk)
      cpus: 8
      nodes: 1

Every key is optional. The section is *advisory defaults for* ``exa pipeline run``: an explicit
``--cluster`` / ``--gpus`` on the command line still wins, and an absent section means exactly
the behaviour that existed before it.

Validation is structural and fail-closed (an unknown key, a bool/negative/non-int count or an empty
cluster name is an error, never ignored) and needs no ``platform.db``, so it is usable as a CI
guard over a whole pack.
"""

from __future__ import annotations

from typing import Any

from examlops.hpc_placement import ResourceAsk

#: The ask keys, in the order ``ResourceAsk`` declares them.
ASK_KEYS: tuple[str, ...] = ("gpus", "cpus", "nodes")
KEYS: frozenset[str] = frozenset({"cluster", *ASK_KEYS})

#: Upper bounds a single training run may ask for. Deliberately generous (a whole large system),
#: they exist so a typo (``gpus: 80000``) is refused at authoring time instead of queueing forever.
_MAX = {"gpus": 65_536, "cpus": 1_048_576, "nodes": 65_536}


def validate_placement_block(block: Any) -> list[str]:
    """Errors in a YAML ``placement:`` block (empty list = valid; ``None``/``{}`` = absent)."""
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["placement must be a mapping"]
    errors = [
        f"unknown placement key {k!r} (allowed: {sorted(KEYS)})" for k in block if k not in KEYS
    ]
    if "cluster" in block:
        cluster = block["cluster"]
        if not isinstance(cluster, str) or not cluster.strip():
            errors.append("placement.cluster must be a non-empty string (a cluster name or 'auto')")
    for key in ASK_KEYS:
        if key not in block:
            continue
        val = block[key]
        if isinstance(val, bool) or not isinstance(val, int):
            errors.append(f"placement.{key} must be an integer, got {type(val).__name__}")
        elif val < (1 if key == "nodes" else 0):
            errors.append(f"placement.{key} must be >= {1 if key == 'nodes' else 0}, got {val}")
        elif val > _MAX[key]:
            errors.append(f"placement.{key}={val} exceeds the sanity cap {_MAX[key]}")
    return errors


def placement_ask(block: dict[str, Any] | None) -> ResourceAsk | None:
    """The ``ResourceAsk`` a valid block states, or ``None`` when it states no ask at all."""
    if not block or not any(k in block for k in ASK_KEYS):
        return None
    ints = {k: int(block[k]) for k in ASK_KEYS if k in block}
    return ResourceAsk(
        gpus=ints.get("gpus", 0), cpus=ints.get("cpus", 0), nodes=ints.get("nodes", 1)
    )


def placement_from_ir(doc: dict[str, Any], train_node: dict[str, Any]) -> dict[str, Any]:
    """The ``placement:`` section a ``training`` IR lowers to (empty = omit the section)."""
    out: dict[str, Any] = {}
    cluster = (doc.get("target") or {}).get("cluster")
    if cluster:
        out["cluster"] = cluster
    for key in ASK_KEYS:
        if key in (train_node.get("resources") or {}):
            out[key] = train_node["resources"][key]
    return out


def split_placement(block: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    """The reverse of :func:`placement_from_ir`: ``(target cluster, train-step resources)``."""
    return block.get("cluster"), {k: block[k] for k in ASK_KEYS if k in block}
