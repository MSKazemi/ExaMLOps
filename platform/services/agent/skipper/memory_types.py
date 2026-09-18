"""SM2 — long-term memory types + store helpers for the Skipper agent.

Ops-agent value order: **procedural > episodic > preference > semantic-KB**. Memory
holds the agent's *experience* + operator *preferences* + *stable tribal knowledge*
only — platform state (versions, drift, cost, audit rows) is queried live and
**pointed to, never copied** (episodes carry FK ids into platform_db, not the rows).

Built directly on the LangGraph ``SqliteStore`` (see design/vision/specs/SM2-*). Each
stored value has a top-level ``text`` field (what the store embeds) plus structured
``data``. Namespaces partition by kind: ``("proc", task_class)``, ``("episode", model)``,
``("pref", operator)``, ``("kb",)``.

Governance (audit + write-gating) is layered on in SM3 (ADR 0034); these helpers are
the mechanism SM3 wraps.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from skipper import config

log = logging.getLogger("skipper.memory_types")

# --- Kinds & namespaces -------------------------------------------------------

KIND_PROC = "proc"
KIND_EPISODE = "episode"
KIND_PREF = "pref"
KIND_KB = "kb"
KINDS = (KIND_PROC, KIND_EPISODE, KIND_PREF, KIND_KB)


def _base_ns(kind: str, scope: str | None) -> tuple[str, ...]:
    if kind not in KINDS:
        raise ValueError(f"unknown memory kind {kind!r}; expected one of {KINDS}")
    return (kind,) if scope is None else (kind, scope)


def namespace(kind: str, scope: str | None = None) -> tuple[str, ...]:
    """Namespace a memory kind is WRITTEN under. With ``scope`` → a specific bucket
    (e.g. task-class / model / operator); without → the kind's prefix (search-all).

    When tenant scoping is enabled (Phase 8, ADR 0105) the active tenant is prefixed
    (``("t:<tenant>", kind[, scope])``); when off — the default — this is byte-for-byte the
    original ``(kind[, scope])``."""
    from skipper import scoping

    return (*scoping.write_prefix(), *_base_ns(kind, scope))


# --- Schemas ------------------------------------------------------------------


class Provenance(BaseModel):
    operator: str = "operator"
    session_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class Procedure(BaseModel):
    """A reusable operational skill distilled from a successful run."""

    task_class: str
    steps: list[str]
    success_conditions: str = ""
    version: int = 1
    deprecated: bool = False
    provenance: Provenance = Field(default_factory=Provenance)


class Incident(BaseModel):
    """A resolved incident — abstraction + FK pointers into platform_db (not copies)."""

    model: str
    symptom: str
    root_cause: str = ""
    resolution: str = ""
    audit_event_id: int | None = None
    drift_snapshot_id: int | None = None
    provenance: Provenance = Field(default_factory=Provenance)


class Preference(BaseModel):
    operator: str
    topic: str
    value: str
    context: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)


class KBFact(BaseModel):
    """Stable tribal knowledge that has no platform_db table."""

    fact: str
    tags: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


# --- Governance: audit every mutation (SM3, ADR 0034) -------------------------


def audit_memory_op(
    action: str, kind: str, scope: str | None, operator: str, digest: str, session_id: str | None
) -> None:
    """Best-effort: record a memory mutation in platform_db.audit_events. Never
    blocks the memory op (audit is a governance record, not a hard dependency)."""
    if not config.AGENT_MEMORY_AUDIT:
        return
    try:
        from examlops.data.audit import audit_best_effort

        # Was `log.debug`, which is invisible at any production log level — a loss nobody could
        # see. `AGENT_MEMORY_AUDIT` is documented as a governance control, so the drop is counted
        # (and logged at WARNING) while still never breaking a memory operation.
        audit_best_effort(
            "agent-memory",
            operator,
            action,
            f"{kind}/{scope}" if scope else kind,
            {"digest": digest, "session_id": session_id},
        )
    except Exception as exc:  # noqa: BLE001 — the import itself can fail without examlops present
        log.warning("memory audit unavailable (%s)", exc)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _verified_operator(fallback: str) -> str:
    """Prefer the request identity; callers cannot forge the audited memory actor."""
    from skipper import scoping

    identity = scoping.request_identity()
    return identity.principal if identity is not None else fallback


# --- Store I/O (pure functions over a BaseStore) ------------------------------


def _put(store: Any, kind: str, scope: str | None, text: str, item: BaseModel) -> str:
    key = uuid.uuid4().hex
    store.put(namespace(kind, scope), key, {"kind": kind, "text": text, "data": item.model_dump()})
    prov = getattr(item, "provenance", None)
    audit_memory_op(
        "memory_record",
        kind,
        scope,
        getattr(prov, "operator", "operator"),
        _digest(text),
        getattr(prov, "session_id", None),
    )
    return key


def record_procedure(
    store: Any,
    task_class: str,
    steps: list[str],
    success_conditions: str = "",
    *,
    operator: str = "operator",
    session_id: str | None = None,
) -> str:
    operator = _verified_operator(operator)
    proc = Procedure(
        task_class=task_class,
        steps=steps,
        success_conditions=success_conditions,
        provenance=Provenance(operator=operator, session_id=session_id),
    )
    text = f"procedure for {task_class}: " + " ; ".join(steps)
    if success_conditions:
        text += f" (success when: {success_conditions})"
    return _put(store, KIND_PROC, task_class, text, proc)


def record_incident(
    store: Any,
    model: str,
    symptom: str,
    root_cause: str = "",
    resolution: str = "",
    *,
    audit_event_id: int | None = None,
    drift_snapshot_id: int | None = None,
    operator: str = "operator",
    session_id: str | None = None,
) -> str:
    operator = _verified_operator(operator)
    inc = Incident(
        model=model,
        symptom=symptom,
        root_cause=root_cause,
        resolution=resolution,
        audit_event_id=audit_event_id,
        drift_snapshot_id=drift_snapshot_id,
        provenance=Provenance(operator=operator, session_id=session_id),
    )
    text = f"incident on {model}: {symptom}. cause: {root_cause}. resolution: {resolution}"
    return _put(store, KIND_EPISODE, model, text, inc)


def record_preference(
    store: Any,
    topic: str,
    value: str,
    *,
    operator: str = "operator",
    context: str | None = None,
    session_id: str | None = None,
) -> str:
    operator = _verified_operator(operator)
    pref = Preference(
        operator=operator,
        topic=topic,
        value=value,
        context=context,
        provenance=Provenance(operator=operator, session_id=session_id),
    )
    text = f"{operator} prefers {topic}: {value}"
    return _put(store, KIND_PREF, operator, text, pref)


def record_kb_fact(
    store: Any,
    fact: str,
    *,
    tags: list[str] | None = None,
    operator: str = "operator",
    session_id: str | None = None,
) -> str:
    operator = _verified_operator(operator)
    kb = KBFact(
        fact=fact, tags=tags or [], provenance=Provenance(operator=operator, session_id=session_id)
    )
    return _put(store, KIND_KB, None, fact, kb)


def recall(
    store: Any,
    kind: str,
    query: str,
    k: int = 5,
    scope: str | None = None,
    *,
    operator: str | None = None,
) -> list[Any]:
    """Semantic search within a memory kind (prefix ``(kind,)`` searches all scopes).

    Deprecated procedures are filtered out. When tenant scoping is on (ADR 0105) this searches the
    operator's authorized tenant + the shared bucket and merges the results (capped at ``k``); with
    scoping off it is a single unprefixed search — unchanged."""
    from skipper import scoping

    base = _base_ns(kind, scope)
    hits: list[Any] = []
    for prefix in scoping.read_prefixes(operator):
        hits.extend(store.search((*prefix, *base), query=query, limit=k))
    if kind == KIND_PROC:
        hits = [h for h in hits if not h.value.get("data", {}).get("deprecated")]
    return hits[:k]


def list_kind(store: Any, kind: str, scope: str | None = None, limit: int = 50) -> list[Any]:
    """List stored memories of a kind (no semantic query) across the readable tenant(s)."""
    from skipper import scoping

    base = _base_ns(kind, scope)
    out: list[Any] = []
    for prefix in scoping.read_prefixes():
        out.extend(store.search((*prefix, *base), limit=limit))
    return out


# --- Lifecycle: enumerate / export / erase (SM3, ADR 0034) --------------------


def stats(store: Any) -> dict[str, int]:
    """Count readable memories per kind in the active owner scope."""
    from skipper import scoping

    return {
        kind: sum(
            len(store.search((*prefix, kind), limit=100000)) for prefix in scoping.read_prefixes()
        )
        for kind in KINDS
    }


def export_all(store: Any, kinds: tuple[str, ...] | None = None) -> dict[str, list[dict]]:
    """Dump readable memories in the active owner scope (GDPR export / inspection)."""
    from skipper import scoping

    out: dict[str, list[dict]] = {}
    for k in kinds or KINDS:
        out[k] = []
        for prefix in scoping.read_prefixes():
            out[k].extend(
                {"namespace": list(it.namespace), "key": it.key, "value": it.value}
                for it in store.search((*prefix, k), limit=100000)
            )
    return out


def erase(store: Any, kind: str, scope: str | None = None, *, operator: str = "operator") -> int:
    """Delete all memories under ``(kind[, scope])`` and audit the deletion.

    Cascade-ready: as derived/summary memories are added they carry a parent id and
    are deleted here too (none exist yet, so a namespace erase is complete). The
    immutable ``audit_events`` log is a *separate* store and is untouched by erasure
    (GDPR-delete vs AI-Act-retain separation, ADR 0034 §R7)."""
    items = store.search(namespace(kind, scope), limit=100000)
    count = 0
    for it in items:
        store.delete(it.namespace, it.key)
        count += 1
    audit_memory_op("memory_erase", kind, scope, operator, f"count={count}", None)
    return count
