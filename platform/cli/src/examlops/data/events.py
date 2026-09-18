"""examlops.data.events — Event backbone — transactional outbox (item 1.3).

Owns these helpers (bodies live here, not in ``platform_db``) — per-domain split (item 4.5) with the
implementation physically relocated. ``platform_db`` re-exports them for back-compat.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, install_write_retry, write_retry

__all__ = [
    "enqueue_event",
    "claim_outbox_batch",
    "mark_event_published",
    "mark_event_failed",
    "outbox_oldest_pending_age",
    "outbox_stats",
    "create_federated_run",
    "get_federated_run",
    "get_federated_sites",
    "get_reasoning_trace",
    "lineage_graph",
    "lineage_impact",
    "list_burst_events",
    "list_federated_rounds",
    "reasoning_usage_summary",
    "record_burst_event",
    "record_cache_event",
    "record_federated_round",
    "record_lineage_event",
    "record_reasoning_usage",
    "record_routing_event",
    "record_structured_output_event",
    "register_federated_site",
    "routing_stats",
    "store_reasoning_trace",
    "structured_output_stats",
]


def enqueue_event(
    topic: str,
    payload: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    actor: str | None = None,
    tenant: str | None = None,
) -> int:
    """Append an event to the transactional outbox (Phase 1 item 1.3); returns its row id.

    Pass an open ``conn`` to enqueue inside an existing transaction so the event and the domain
    write commit atomically (the whole point of an outbox — no lost or phantom events). Without
    ``conn`` it opens its own hardened transaction. ``actor``/``tenant`` travel into the event's
    CloudEvents envelope (ADR 0124); unset they keep the table defaults.
    """
    from examlops.events.envelope import current_traceparent  # noqa: PLC0415

    payload_json = json.dumps(payload, default=str)
    sql = "INSERT INTO event_outbox (topic, payload, actor, tenant, traceparent) VALUES (?,?,?,?,?)"
    # Captured here, inside the request that made the change — not at relay time, when the only
    # span around is the relay's own and a consumer would continue the wrong trace (plan P2.5).
    params = (topic, payload_json, actor or "system", tenant or "default", current_traceparent())
    if conn is not None:
        cur = conn.execute(sql, params)
        return int(cur.lastrowid or 0)

    init_db()

    def _insert() -> int:
        with get_db() as c:
            cur = c.execute(sql, params)
            return int(cur.lastrowid or 0)

    return write_retry(_insert)


def claim_outbox_batch(
    limit: int = 100, *, visibility_s: int = 300, max_attempts: int = 5
) -> list[dict[str, Any]]:
    """Atomically claim up to ``limit`` unpublished events for one relay worker (item 1.3).

    Under a RESERVED write lock: select rows that are unpublished AND not currently claimed (or
    whose claim has expired past ``visibility_s`` — a crashed relay's rows become reclaimable),
    stamp ``claimed_at = now`` + bump ``attempts`` on exactly those, and return them. Because the
    claim hides rows from other relays until they're published or the lease expires, two healthy
    concurrent relays do not publish the same event. A relay crash after broker publication can
    cause a replay after the lease expires; consumers deduplicate the stable outbox event ID. The
    relay calls :func:`mark_event_published` (done) or :func:`mark_event_failed` (clears the claim
    for immediate retry) per row. Rows at ``max_attempts`` remain in the outbox as poison evidence
    but are not claimed again automatically.
    """

    if limit <= 0:
        return []
    if max_attempts <= 0:
        raise ValueError("max_attempts must be greater than zero")

    def _claim() -> list[dict[str, Any]]:
        with _immediate_write("outbox") as conn:
            rows = conn.execute(
                "SELECT id, topic, payload, attempts, created_at, actor, tenant, traceparent "
                "FROM event_outbox "
                "WHERE published_at IS NULL "
                "  AND attempts < ? "
                "  AND (claimed_at IS NULL "
                "       OR claimed_at <= datetime(CURRENT_TIMESTAMP, ?)) "
                "ORDER BY id ASC LIMIT ?",
                (max_attempts, f"-{max(0, int(visibility_s))} seconds", limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    f"UPDATE event_outbox "
                    f"   SET attempts = attempts + 1, claimed_at = CURRENT_TIMESTAMP "
                    f" WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
            return [dict(r) for r in rows]

    return write_retry(_claim)


def mark_event_published(event_id: int) -> None:
    def _mark() -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE event_outbox SET published_at = CURRENT_TIMESTAMP, last_error = NULL "
                "WHERE id = ?",
                (event_id,),
            )

    write_retry(_mark)


def mark_event_failed(event_id: int, error: str) -> None:
    def _mark() -> None:
        with get_db() as conn:
            # Clear the claim so the row is immediately reclaimable for retry (don't wait out
            # the visibility lease on a known failure).
            conn.execute(
                "UPDATE event_outbox SET last_error = ?, claimed_at = NULL WHERE id = ?",
                (error[:500], event_id),
            )

    write_retry(_mark)


def defer_outbox_claim(event_ids: list[int], error: str) -> None:
    """Return claimed rows to the queue **without spending an attempt** (the broker is absent).

    ``attempts`` is the poison budget: it exists to stop an event the broker rejects and always
    will. A transport outage rejects nothing, so charging it there strands the whole backlog as
    poison after a few cycles — the backbone chaos drill's combined-failure section is what found
    it. The claim is cleared so the rows are relayed as soon as the broker answers again.
    """
    if not event_ids:
        return

    def _defer() -> None:
        with get_db() as conn:
            conn.execute(
                f"UPDATE event_outbox "
                f"   SET claimed_at = NULL, last_error = ?, "
                f"       attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END "
                f" WHERE published_at IS NULL AND id IN ({','.join('?' * len(event_ids))})",
                (error[:500], *event_ids),
            )

    write_retry(_defer)


def outbox_stats(*, max_attempts: int | None = None) -> dict[str, int]:
    """Counts for monitoring the relay: pending vs published vs poison (attempts exhausted)."""
    if max_attempts is None:
        try:
            max_attempts = int(os.getenv("EXAMLOPS_EVENT_MAX_ATTEMPTS", "5"))
        except ValueError:
            max_attempts = 5
    max_attempts = max(1, max_attempts)
    with get_db() as conn:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN published_at IS NULL THEN 1 ELSE 0 END) AS pending, "
            "  SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS published, "
            "  SUM(CASE WHEN published_at IS NULL AND attempts >= ? THEN 1 ELSE 0 END) AS poison "
            "FROM event_outbox",
            (max_attempts,),
        ).fetchone()
    return {
        "pending": row["pending"] or 0,
        "published": row["published"] or 0,
        "poison": row["poison"] or 0,
    }


def outbox_oldest_pending_age() -> float | None:
    """Seconds the oldest unpublished event has waited, or ``None`` when nothing is pending.

    Kept out of :func:`outbox_stats` on purpose: that dict is counts, and callers sum it.
    A count cannot tell a relay that is keeping up from one that stopped an hour ago; this can.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(created_at) FROM event_outbox WHERE published_at IS NULL"
        ).fetchone()
    oldest = row[0] if row else None
    if oldest is None:
        return None
    if isinstance(oldest, datetime):
        created = oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)
    else:
        created = datetime.fromisoformat(str(oldest).replace(" ", "T"))
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - created).total_seconds())


def create_federated_run(
    run_id: str,
    strategy: str,
    *,
    dp_enabled: bool = False,
    secure_agg: bool = False,
    delta: float = 0.0,
    epsilon_per_round: float = 0.0,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO federated_runs
                   (run_id, strategy, dp_enabled, secure_agg, delta, epsilon_per_round, status)
               VALUES (?,?,?,?,?,?, 'initialized')""",
            (
                run_id,
                strategy,
                1 if dp_enabled else 0,
                1 if secure_agg else 0,
                delta,
                epsilon_per_round,
            ),
        )


def get_federated_run(run_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM federated_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_federated_sites(run_id: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM federated_sites WHERE run_id=? ORDER BY site", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_reasoning_trace(request_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM reasoning_traces WHERE request_id=?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


#: Facts a run's events record; a run shows the newest non-empty value of each.
_RUN_FACTS = ("dataset_revision", "mlflow_run_id", "model_version", "trace_id")


def lineage_graph(model: str) -> dict[str, Any]:
    """Return upstream (datasets/runs) + downstream (deployments) nodes for a model.

    One entry per **run**, not per event: a training run emits ``START``, then ``COMPLETE`` or
    ``FAIL``, under one run id. Each entry is the run's newest event, carrying ``events`` (its
    event types, oldest first) and the newest non-empty value of each fact any of its events
    recorded — a ``FAIL`` after ``COMPLETE`` does not erase the dataset revision the run trained
    on, which the synthetic-only promotion gate reads from here.
    """
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM lineage_events WHERE model=? ORDER BY ts DESC, id DESC", (model,)
        ).fetchall()
        runs: dict[str, dict[str, Any]] = {}
        for r in rows:  # newest first
            row = dict(r)
            run = runs.get(row["run_id"])
            if run is None:
                runs[row["run_id"]] = {**row, "events": [row["event_type"]]}
                continue
            run["events"].insert(0, row["event_type"])
            for fact in _RUN_FACTS:
                if run.get(fact) is None and row.get(fact) is not None:
                    run[fact] = row[fact]
        io_rows: list[dict[str, Any]] = []
        for rid in runs:
            io_rows.extend(
                dict(r)
                for r in conn.execute("SELECT * FROM lineage_io WHERE run_id=?", (rid,)).fetchall()
            )
    return {
        "model": model,
        "runs": list(runs.values()),
        "upstream": [r for r in io_rows if r["direction"] == "input"],
        "downstream": [r for r in io_rows if r["direction"] == "output"],
    }


def lineage_run_seen(run_id: str) -> bool:
    """Whether any lineage event has been recorded for ``run_id``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM lineage_events WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone()
    return row is not None


def lineage_run_for_mlflow_run(mlflow_run_id: str) -> dict[str, Any] | None:
    """The training lineage run behind an MLflow run — its ``run_id``, ``job`` and ``model`` — or
    ``None``. How a fact learned after the run (its cost) finds the run to attach to."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            # The pattern is a *parameter*, not part of the statement: psycopg reads a literal
            # `%` in a parameterised query as a placeholder, so `LIKE 'train:%'` raised
            # "only '%s', '%b', '%t' are allowed as placeholders" on every Postgres install — and
            # `attach_run_cost` swallows it, so cost and carbon lineage was silently never recorded.
            "SELECT run_id, job, model FROM lineage_events "
            "WHERE mlflow_run_id=? AND job LIKE ? ORDER BY id DESC LIMIT 1",
            (mlflow_run_id, "train:%"),
        ).fetchone()
    return dict(row) if row else None


def lineage_impact(dataset_revision: str) -> list[dict[str, Any]]:
    """List every model version derived (transitively) from a dataset revision."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT model, model_version, run_id, mlflow_run_id
               FROM lineage_events
               WHERE dataset_revision=? AND model IS NOT NULL
               ORDER BY model, model_version""",
            (dataset_revision,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_burst_events(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM burst_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_federated_rounds(run_id: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM federated_rounds WHERE run_id=? ORDER BY round_num", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def reasoning_usage_summary(model: str | None = None, tenant: str | None = None) -> dict[str, Any]:
    init_db()
    clauses, params = [], []
    if model:
        clauses.append("model=?")
        params.append(model)
    if tenant:
        clauses.append("tenant=?")
        params.append(tenant)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(reasoning_cost),0) AS reasoning_cost,
                       COALESCE(SUM(output_cost),0) AS output_cost
                FROM reasoning_usage{where}""",
            tuple(params),
        ).fetchone()
    return dict(row)


def record_burst_event(
    workload: str,
    *,
    from_pool: str | None,
    to_pool: str | None,
    residency: str | None,
    allowed: bool,
    reason: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO burst_events
                   (workload, from_pool, to_pool, residency, allowed, reason)
               VALUES (?,?,?,?,?,?)""",
            (workload, from_pool, to_pool, residency, 1 if allowed else 0, reason),
        )


def record_cache_event(
    tenant: str,
    model: str,
    *,
    hit: bool,
    similarity: float | None = None,
    tokens_saved: int = 0,
    cost_saved: float = 0.0,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO cache_events (tenant, model, hit, similarity, tokens_saved, cost_saved)
               VALUES (?,?,?,?,?,?)""",
            (tenant, model, 1 if hit else 0, similarity, tokens_saved, cost_saved),
        )


def record_federated_round(
    run_id: str,
    round_num: int,
    global_metric: float | None,
    sites_participated: int,
    epsilon: float,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO federated_rounds
                   (run_id, round_num, global_metric, sites_participated, epsilon)
               VALUES (?,?,?,?,?)""",
            (run_id, round_num, global_metric, sites_participated, epsilon),
        )
        conn.execute(
            """UPDATE federated_runs SET rounds_completed=?, epsilon=?, status='running',
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=?""",
            (round_num, epsilon, run_id),
        )


def record_lineage_event(
    run_id: str,
    job: str,
    event_type: str,
    *,
    inputs: list[dict[str, str]] | None = None,
    outputs: list[dict[str, str]] | None = None,
    dataset_revision: str | None = None,
    mlflow_run_id: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    trace_id: str | None = None,
    facets: dict[str, Any] | None = None,
) -> None:
    """Upsert a lineage run event + its I/O nodes (the operational source of truth)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO lineage_events
                   (run_id, job, event_type, dataset_revision, mlflow_run_id,
                    model, model_version, trace_id, facets_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                job,
                event_type,
                dataset_revision,
                mlflow_run_id,
                model,
                model_version,
                trace_id,
                json.dumps(facets) if facets else None,
            ),
        )
        for direction, nodes in (("input", inputs or []), ("output", outputs or [])):
            for node in nodes:
                conn.execute(
                    """INSERT OR IGNORE INTO lineage_io
                           (run_id, direction, node_type, node_name)
                       VALUES (?,?,?,?)""",
                    (run_id, direction, node.get("type", "dataset"), node["name"]),
                )


def record_reasoning_usage(
    model: str,
    reasoning_tokens: int,
    output_tokens: int,
    reasoning_cost: float,
    output_cost: float,
    *,
    tenant: str = "default",
    request_id: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO reasoning_usage
                   (model, tenant, request_id, reasoning_tokens, output_tokens,
                    reasoning_cost, output_cost)
               VALUES (?,?,?,?,?,?,?)""",
            (
                model,
                tenant,
                request_id,
                reasoning_tokens,
                output_tokens,
                reasoning_cost,
                output_cost,
            ),
        )


def record_routing_event(
    model: str,
    prefix_key: str | None,
    replica: str,
    decision: str,
    hit: bool,
    *,
    tenant: str = "default",
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO routing_events (model, tenant, prefix_key, replica, decision, hit)
               VALUES (?,?,?,?,?,?)""",
            (model, tenant, prefix_key, replica, decision, 1 if hit else 0),
        )


def record_structured_output_event(
    outcome: str,
    *,
    model: str | None = None,
    tenant: str = "default",
    constrained: bool = False,
) -> None:
    """Record one structured-output attempt. ``constrained`` says whether the schema constrained
    the decoder (ADR 0035 clause 1) or only judged the answer afterwards."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO structured_output_events (model, tenant, outcome, constrained) "
            "VALUES (?,?,?,?)",
            (model, tenant, outcome, int(constrained)),
        )


def register_federated_site(run_id: str, site: str, authorized: bool) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO federated_sites (run_id, site, authorized)
               VALUES (?,?,?)""",
            (run_id, site, 1 if authorized else 0),
        )


def routing_stats(model: str, tenant: str | None = None) -> dict[str, Any]:
    init_db()
    clauses = ["model=?"]
    params: list[Any] = [model]
    if tenant:
        clauses.append("tenant=?")
        params.append(tenant)
    where = " AND ".join(clauses)
    with get_db() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS total, COALESCE(SUM(hit),0) AS hits
                FROM routing_events WHERE {where}""",
            tuple(params),
        ).fetchone()
        by_decision = conn.execute(
            f"SELECT decision, COUNT(*) AS n FROM routing_events WHERE {where} GROUP BY decision",
            tuple(params),
        ).fetchall()
    total = row["total"]
    return {
        "total": total,
        "hits": row["hits"],
        "hit_rate": (row["hits"] / total) if total else 0.0,
        "by_decision": {r["decision"]: r["n"] for r in by_decision},
    }


def store_reasoning_trace(
    request_id: str,
    redacted_trace: str,
    *,
    tenant: str = "default",
    expires_at: float | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reasoning_traces
                   (request_id, tenant, redacted_trace, expires_at)
               VALUES (?,?,?,?)""",
            (request_id, tenant, redacted_trace, expires_at),
        )


def structured_output_stats() -> dict[str, int]:
    """Counts per outcome, plus ``constrained``: how many of them decoded under the schema
    rather than being checked after the fact (ADR 0035 clause 1)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM structured_output_events GROUP BY outcome"
        ).fetchall()
        constrained = conn.execute(
            "SELECT COUNT(*) AS n FROM structured_output_events WHERE constrained=1"
        ).fetchone()
    stats = {r["outcome"]: r["n"] for r in rows}
    stats["constrained"] = constrained["n"] if constrained else 0
    return stats


def inbox_seen(consumer: str, event_id: str) -> bool:
    """True when ``consumer`` has already handled ``event_id`` (ADR 0124 consumer inbox)."""
    init_db()  # schema-once; a consumer may be the first thing a process touches
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM event_inbox WHERE consumer=? AND event_id=?", (consumer, event_id)
        ).fetchone()
    return row is not None


def inbox_record(consumer: str, event_id: str) -> bool:
    """Record that ``consumer`` handled ``event_id``; False when it was already recorded."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO event_inbox (consumer, event_id) VALUES (?, ?)",
            (consumer, event_id),
        )
        return bool(cur.rowcount)


# Wrap every mutating helper above (including the inbox writer) with write_retry (item 0.4).
install_write_retry(__name__)
