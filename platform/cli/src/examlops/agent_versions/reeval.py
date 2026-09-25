"""Re-evaluate dependent agents when a followed model moves (ADR 0146 decision 2, verification 6).

A ``follow`` binding lets a model promotion reach an agent without a new agent version - which
is exactly why the promotion must not reach it *unexamined*. When the alias a binding follows
moves to a different model version, every agent version that follows it gets a re-evaluation
entry, and each enqueue is written to the hash-chained audit log (the evidence chain).

* **non-blocking** (default): the entry is a to-do for the evaluation pipeline; the agent keeps
  following the alias.
* **blocking** (``policy.reeval_on_follow: blocking`` in the manifest): until the entry is
  resolved ``passed``, the agent snapshot pins that binding to the model version it followed
  *before* the move, so the runtime keeps running the evaluated combination. A ``failed``
  resolution keeps the pin; the agent must be re-registered or the model rolled back.

Hooked into :func:`examlops.events.alias_changed`, the one funnel every model-alias move already
calls, so no promotion surface can skip it. Best-effort there: an enqueue failure is logged and
the model promotion - which already happened in MLflow - stands.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from examlops.data import agent_versions as store
from examlops.data.audit import audit_best_effort

__all__ = [
    "OUTCOMES",
    "dependents",
    "list_reevals",
    "on_model_alias_changed",
    "pinned_overrides",
    "resolve",
    "servable_key",
]

logger = logging.getLogger(__name__)
_SOURCE = "agent-versions"
OUTCOMES = ("passed", "failed")
_BATCH = 500


def servable_key(servable: str) -> str:
    """``gen://Qwen3-32B`` -> ``qwen3-32b``: the lower-case model key the registry uses."""
    s = servable.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.strip("/").lower()


def dependents(model: str, alias: str) -> list[dict[str, Any]]:
    """Every registered agent version with a ``follow`` binding on ``model@alias``.

    Walks the registry in bounded, keyed pages (a coarse SQL pre-filter on the model name, then
    an exact check on the parsed manifest), so it scales with the number of dependents rather
    than loading the whole registry at once.
    """
    key = servable_key(model)
    out: list[dict[str, Any]] = []
    after = ""
    while True:
        page = store.versions_mentioning(key.split("/")[-1], after=after, batch=_BATCH)
        if not page:
            break
        for row in page:
            for m in row["manifest"].get("models", []):
                if (
                    m.get("binding") == "follow"
                    and servable_key(str(m.get("servable", ""))) == key
                    and str(m.get("alias", "")).lower() == alias.lower()
                ):
                    out.append({"row": row, "binding": m})
                    break
        after = page[-1]["version_id"]
        if len(page) < _BATCH:
            break
    return out


def on_model_alias_changed(
    model: str,
    alias: str,
    version: str | int | None,
    *,
    previous_version: str | int | None = None,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Enqueue re-evaluation of every agent version following ``model@alias``.

    A no-op when the resolved version did not change. Returns the entries created (an entry
    that already exists - the same move announced twice - is not created again or re-audited).
    """
    new = None if version is None else str(version)
    old = None if previous_version is None else str(previous_version)
    if new is not None and new == old:
        return []
    who = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    created: list[dict[str, Any]] = []
    for dep in dependents(model, alias):
        row, binding = dep["row"], dep["binding"]
        blocking = (row["manifest"].get("policy") or {}).get("reeval_on_follow") == "blocking"
        made, entry = store.enqueue_reeval(
            row["agent"],
            row["version_id"],
            servable_key(str(binding["servable"])),
            str(binding["alias"]),
            new,
            old,
            blocking=blocking,
            actor=who,
        )
        if not made:
            continue
        audit_best_effort(
            _SOURCE,
            who,
            "agent_reeval_enqueued",
            f"{row['agent']}:{row['version_id']}",
            {
                "version_id": row["version_id"],
                "role": binding.get("role"),
                "servable": binding["servable"],
                "alias": binding["alias"],
                "model_version": new,
                "previous_version": old,
                "blocking": blocking,
                "reeval_id": entry["id"],
            },
        )
        created.append(entry)
    return created


def list_reevals(
    *, agent: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    return store.list_reevals(agent=agent, status=status, limit=limit)


def resolve(
    reeval_id: int, outcome: str, *, actor: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Close a pending re-evaluation as ``passed`` or ``failed`` (audited).

    Raises ``ValueError`` for an unknown outcome and ``LookupError`` when the entry is unknown
    or already closed (closing is not repeatable: the first verdict stands).
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    who = actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    row = store.resolve_reeval(reeval_id, outcome, reason=reason, actor=who)
    if row is None:
        raise LookupError(f"no pending re-evaluation with id {reeval_id}")
    audit_best_effort(
        _SOURCE,
        who,
        "agent_reeval_resolved",
        f"{row['agent']}:{row['version_id']}",
        {
            "version_id": row["version_id"],
            "reeval_id": row["id"],
            "outcome": outcome,
            "reason": reason,
            "model_version": row["model_version"],
        },
    )
    return {"ok": True, **row}


def pinned_overrides() -> dict[str, dict[str, str | None]]:
    """``{version_id: {servable_key@alias: previous model version}}`` for blocking entries.

    What the agent snapshot uses to hold a blocking agent on the model version it was evaluated
    with until its re-evaluation passes. A ``failed`` entry keeps holding; only ``passed`` lifts.
    The oldest open entry per binding wins - it names the last version that was evaluated.
    """
    out: dict[str, dict[str, str | None]] = {}
    after = 0
    # Paged to exhaustion: a pin that fell outside one fixed window would silently let a
    # blocking agent follow a model version nobody has evaluated it with.
    while True:
        page = store.open_blocking_reevals(after_id=after, limit=_BATCH)  # oldest first, SQL
        for r in page:
            out.setdefault(r["version_id"], {}).setdefault(
                f"{r['servable']}@{r['alias']}", r["previous_version"]
            )
        if len(page) < _BATCH:
            return out
        after = int(page[-1]["id"])
