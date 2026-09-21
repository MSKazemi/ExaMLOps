"""examlops.data.slo_kind_specs - storage and read-only telemetry sources for SLOSpecs (ADR 0148 d3).

Policy (validation, evaluation, the gate) lives in :mod:`examlops.slo.specs`; this module only
touches ``slo_kind_specs`` / ``slo_spec_verdicts`` and *reads* the ledgers a spec can be judged
against. It never invents a value: a ledger with no rows yields empty observations, which the
evaluator turns into ``no_verdict``.

Sources, stated honestly:

* predictive -> ``gateway_calls`` rows that carry a measured ``latency_ms`` (the only per-request
  latency the platform persists; that table has no tenant column, so it is only used for the
  ``default`` tenant) and ``slo_samples`` of the model's ``availability`` SLOs.
* agentic    -> ended ``agent_sessions`` (job completion time, cost). Task success and
  intervention are *not* recorded by the platform, so they are never sourced here.
* generative -> nothing: TTFT/TPOT are not persisted.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

__all__ = [
    "agent_session_observations",
    "availability_observations",
    "gateway_observations",
    "get",
    "init_db",
    "latest_verdict",
    "list_specs",
    "put",
    "record_verdict",
]

_SPEC_COLS = "servable, kind, tenant, version, spec_json, updated_at, updated_by"


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["spec"] = json.loads(d.pop("spec_json"))
    return d


def put(
    servable: str, kind: str, tenant: str, spec: dict[str, Any], *, actor: str | None = None
) -> tuple[str, int]:
    """Insert or replace one spec. Returns ``(state, version)``.

    ``state`` is ``created``, ``updated`` (objectives changed, version bumped) or ``unchanged``
    (identical objectives: nothing written, version kept).
    """
    blob = json.dumps(spec, sort_keys=True)

    def _do() -> tuple[str, int]:
        init_db()
        with _immediate_write("slo_kind_specs") as conn:
            cur = conn.execute(
                "SELECT version, spec_json FROM slo_kind_specs "
                "WHERE servable=? AND kind=? AND tenant=?",
                (servable, kind, tenant),
            ).fetchone()
            if cur is None:
                conn.execute(
                    f"INSERT INTO slo_kind_specs ({_SPEC_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (servable, kind, tenant, 1, blob, time.time(), actor),
                )
                return "created", 1
            if cur[1] == blob:
                return "unchanged", int(cur[0])
            version = int(cur[0]) + 1
            conn.execute(
                "UPDATE slo_kind_specs SET version=?, spec_json=?, updated_at=?, updated_by=? "
                "WHERE servable=? AND kind=? AND tenant=?",
                (version, blob, time.time(), actor, servable, kind, tenant),
            )
            return "updated", version

    return write_retry(_do)


def get(servable: str, kind: str, tenant: str = "default") -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                f"SELECT {_SPEC_COLS} FROM slo_kind_specs WHERE servable=? AND kind=? AND tenant=?",
                (servable, kind, tenant),
            ).fetchone()
            return _row(r) if r else None

    return write_retry(_do)


def list_specs(
    servable: str | None = None, kind: str | None = None, tenant: str | None = None
) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        where: list[str] = []
        args: list[Any] = []
        for col, val in (("servable", servable), ("kind", kind), ("tenant", tenant)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        sql = f"SELECT {_SPEC_COLS} FROM slo_kind_specs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with get_db() as conn:
            rows = conn.execute(sql + " ORDER BY servable, kind, tenant", args).fetchall()
            return [_row(r) for r in rows]

    return write_retry(_do)


# -- recorded verdicts (append-only) ---------------------------------------------------------


def record_verdict(
    servable: str,
    kind: str,
    tenant: str,
    spec_version: int,
    verdict: str,
    evidence: dict[str, Any],
    *,
    source: str,
    actor: str | None = None,
) -> int:
    def _do() -> int:
        init_db()
        with _immediate_write("slo_spec_verdicts") as conn:
            conn.execute(
                "INSERT INTO slo_spec_verdicts (servable, kind, tenant, spec_version, verdict, "
                "evidence_json, source, ts, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    servable,
                    kind,
                    tenant,
                    spec_version,
                    verdict,
                    json.dumps(evidence, sort_keys=True, default=str),
                    source,
                    time.time(),
                    actor,
                ),
            )
            row = conn.execute(
                "SELECT MAX(id) FROM slo_spec_verdicts WHERE servable=? AND kind=? AND tenant=?",
                (servable, kind, tenant),
            ).fetchone()
            return int(row[0])

    return write_retry(_do)


def latest_verdict(servable: str, kind: str, tenant: str = "default") -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT id, servable, kind, tenant, spec_version, verdict, evidence_json, "
                "source, ts, actor FROM slo_spec_verdicts "
                "WHERE servable=? AND kind=? AND tenant=? ORDER BY id DESC LIMIT 1",
                (servable, kind, tenant),
            ).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    return write_retry(_do)


# -- read-only telemetry sources -------------------------------------------------------------


def _since(window_days: float) -> str:
    return datetime.fromtimestamp(time.time() - window_days * 86400.0, UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def gateway_observations(model: str, window_days: float) -> dict[str, Any]:
    """Measured gateway calls for ``model``: latencies of successful calls + request/error counts."""

    def _do() -> dict[str, Any]:
        init_db()
        since = _since(window_days)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT latency_ms, error FROM gateway_calls "
                "WHERE model=? AND ts>=? AND latency_ms IS NOT NULL",
                (model, since),
            ).fetchall()
        latencies = [float(r[0]) for r in rows if not r[1]]
        return {
            "latency_ms": latencies,
            "requests": len(rows),
            "errors": sum(1 for r in rows if r[1]),
        }

    return write_retry(_do)


def availability_observations(model: str, tenant: str, window_days: float) -> dict[str, Any]:
    """Summed good/total of the model's C6 ``availability`` SLI samples in the window."""

    def _do() -> dict[str, Any]:
        init_db()
        since = _since(window_days)
        with get_db() as conn:
            r = conn.execute(
                "SELECT COALESCE(SUM(s.good), 0), COALESCE(SUM(s.total), 0) "
                "FROM slo_samples s JOIN slo_specs p "
                "ON p.model=s.model AND p.tenant=s.tenant AND p.name=s.name "
                "WHERE s.model=? AND s.tenant=? AND p.sli_source='availability' AND s.ts>=?",
                (model, tenant, since),
            ).fetchone()
        return {"availability_good": float(r[0]), "availability_total": float(r[1])}

    return write_retry(_do)


def agent_session_observations(agent: str, tenant: str, window_days: float) -> dict[str, Any]:
    """Ended sessions of ``agent``: per-task ``jct_s`` and ``cost_usd`` (no success, no interventions)."""

    def _do() -> dict[str, Any]:
        init_db()
        since = _since(window_days)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT started_at, ended_at, cost_usd FROM agent_sessions "
                "WHERE agent=? AND tenant=? AND ended_at IS NOT NULL AND started_at>=?",
                (agent, tenant, since),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for started, ended, cost in rows:
            try:
                jct = (_ts(ended) - _ts(started)).total_seconds()
            except (TypeError, ValueError):
                continue
            tasks.append({"jct_s": jct, "cost_usd": float(cost or 0.0)})
        return {"tasks": tasks}

    return write_retry(_do)


def _ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))
