"""examlops.data.serving — Serving + autoscaling.

The traffic-split + promotion-rule helpers now **physically live here** (per-domain split, item 4.5);
the remaining serving-domain helpers (autoscale/challenger/scale-events) are still proxied to
``platform_db`` via ``__getattr__`` until their bodies move too. ``install_write_retry`` re-applies
the item-0.4 auto-wrapping to the mutating helpers owned here; ``platform_db`` re-exports them.
"""

from __future__ import annotations

import json
from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry

__all__ = [
    "delete_llm_endpoint",
    "get_llm_endpoint",
    "list_llm_endpoints",
    "set_llm_endpoint_state",
    "upsert_llm_endpoint",
    "set_traffic_rules",
    "get_traffic_rules",
    "set_promotion_rule",
    "get_promotion_rule",
    "disable_challenger",
    "get_autoscale_config",
    "get_challenger_config",
    "get_challenger_samples",
    "set_challenger_judge_scores",
    "get_device_pools",
    "list_autoscale_configs",
    "list_challenger_configs",
    "list_scale_events",
    "record_challenger_sample",
    "record_scale_event",
    "set_autoscale_config",
    "set_challenger_config",
]


def set_traffic_rules(model: str, rules: dict[str, int], updated_by: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO traffic_rules (model, rules, updated_by) VALUES (?,?,?)",
            (model, json.dumps(rules), updated_by),
        )


def get_traffic_rules(model: str) -> dict[str, int] | None:
    with get_db() as conn:
        row = conn.execute("SELECT rules FROM traffic_rules WHERE model=?", (model,)).fetchone()
    if row is None:
        return None
    return json.loads(row["rules"])


def set_promotion_rule(
    model: str,
    metric: str,
    operator: str,
    threshold: float,
    from_alias: str = "Staging",
    to_alias: str = "Production",
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO promotion_rules
               (model, metric, operator, threshold, from_alias, to_alias)
               VALUES (?,?,?,?,?,?)""",
            (model, metric, operator, threshold, from_alias, to_alias),
        )


def get_promotion_rule(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM promotion_rules WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


# Re-apply the item-0.4 auto-wrapping to the mutating helpers owned by this module.


def disable_challenger(model: str, *, updated_by: str | None = None) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE challenger_config SET enabled=0, updated_at=CURRENT_TIMESTAMP, "
            "updated_by=? WHERE model=?",
            (updated_by, model),
        )


def get_autoscale_config(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM autoscale_config WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def get_challenger_config(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM challenger_config WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def get_challenger_samples(
    model: str, *, tenant: str = "default", labelled_only: bool = False, last_n: int = 5000
) -> list[dict[str, Any]]:
    """Recent champion/challenger samples for scoring."""
    init_db()
    clause = "WHERE model=? AND tenant=?"
    params: list[Any] = [model, tenant]
    if labelled_only:
        clause += " AND label IS NOT NULL"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM challenger_samples {clause} ORDER BY id DESC LIMIT ?",
            (*params, last_n),
        ).fetchall()
    return [dict(r) for r in rows]


def get_device_pools(
    target: str | None = None,
    accelerator: str | None = None,
    status: str | None = "active",
) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM device_pools WHERE 1=1"
    params: list[Any] = []
    if target:
        q += " AND target=?"
        params.append(target)
    if accelerator:
        q += " AND accelerator=?"
        params.append(accelerator)
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY cost_per_hour, name"
    with get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["capabilities"] = json.loads(d["capabilities"]) if d.get("capabilities") else []
        d["supports_fractions"] = bool(d["supports_fractions"])
        out.append(d)
    return out


def list_autoscale_configs() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM autoscale_config ORDER BY model").fetchall()
    return [dict(r) for r in rows]


def list_challenger_configs(*, tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    where = "WHERE tenant=?" if tenant else ""
    params = (tenant,) if tenant else ()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM challenger_config {where} ORDER BY model", params
        ).fetchall()
    return [dict(r) for r in rows]


def list_scale_events(model: str, *, last_n: int = 50) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scale_events WHERE model=? ORDER BY id DESC LIMIT ?", (model, last_n)
        ).fetchall()
    return [dict(r) for r in rows]


def record_challenger_sample(
    model: str,
    *,
    tenant: str = "default",
    request_hash: str | None = None,
    champion_pred: float | None = None,
    challenger_pred: float | None = None,
    label: float | None = None,
) -> None:
    """Log one champion vs challenger prediction pair (R4)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO challenger_samples
                   (model, tenant, request_hash, champion_pred, challenger_pred, label)
               VALUES (?,?,?,?,?,?)""",
            (model, tenant, request_hash, champion_pred, challenger_pred, label),
        )


def set_challenger_judge_scores(
    sample_id: int, *, champion: float | None, challenger: float | None, judge_model: str
) -> None:
    """Attach a C2 judge's scores to one challenger sample (ADR 0024 clause 2).

    Written to their own columns, never to ``label``. A judge's opinion is not ground truth, and
    a judged sample indistinguishable from a measured one turns the scoreboard into a mixture
    nobody can separate afterwards — the promotion decision would rest on evidence of unknown
    provenance.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE challenger_samples SET champion_judge=?, challenger_judge=?, judge_model=? "
            "WHERE id=?",
            (champion, challenger, judge_model, sample_id),
        )


def record_scale_event(
    model: str,
    from_replicas: int,
    to_replicas: int,
    *,
    tenant: str = "default",
    reason: str | None = None,
    metric_value: float | None = None,
    cold_start_s: float | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO scale_events
                   (model, tenant, from_replicas, to_replicas, reason, metric_value, cold_start_s)
               VALUES (?,?,?,?,?,?,?)""",
            (model, tenant, from_replicas, to_replicas, reason, metric_value, cold_start_s),
        )


def set_autoscale_config(
    model: str,
    min_replicas: int | None = None,
    max_replicas: int | None = None,
    **kw: Any,
) -> None:
    """Upsert a per-model autoscale policy (R1).

    Backward-compatible with the Phase-24 positional stub
    (``set_autoscale_config(model, min_replicas, max_replicas, target_ongoing=..., updated_by=...)``)
    and the E5 keyword form (``set_autoscale_config(model, target_metric=..., warm_pool=..., ...)``).
    """
    init_db()
    if min_replicas is not None:
        kw["min_replicas"] = min_replicas
    if max_replicas is not None:
        kw["max_replicas"] = max_replicas
    defaults = {
        "tenant": "default",
        "min_replicas": 1,
        "max_replicas": 4,
        "target_ongoing": 8,
        "target_metric": "queue_depth",
        "target_value": 10.0,
        "scale_to_zero_after_s": 0,
        "warm_pool": 0,
        "stabilization_s": 30,
        "cooldown_s": 60,
        "gpu_fraction": 1.0,
        "enabled": 1,
        "updated_by": None,
    }
    existing = get_autoscale_config(model) or {}
    cfg = {**defaults, **{k: existing.get(k) for k in defaults if k in existing}, **kw}
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO autoscale_config
                   (model, tenant, min_replicas, max_replicas, target_ongoing, target_metric,
                    target_value, scale_to_zero_after_s, warm_pool, stabilization_s, cooldown_s,
                    gpu_fraction, enabled, updated_by, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)""",
            (
                model,
                cfg["tenant"],
                cfg["min_replicas"],
                cfg["max_replicas"],
                cfg["target_ongoing"],
                cfg["target_metric"],
                cfg["target_value"],
                cfg["scale_to_zero_after_s"],
                cfg["warm_pool"],
                cfg["stabilization_s"],
                cfg["cooldown_s"],
                cfg["gpu_fraction"],
                int(cfg["enabled"]),
                cfg["updated_by"],
            ),
        )


def set_challenger_config(
    model: str,
    challenger_version: str,
    *,
    tenant: str = "default",
    mirror_pct: int = 100,
    min_delta: float = 0.0,
    alpha: float = 0.05,
    min_samples: int = 100,
    auto_promote: bool = False,
    enabled: bool = True,
    updated_by: str | None = None,
) -> None:
    """Enable/configure a challenger for a model (R1/R5)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO challenger_config
                   (model, tenant, challenger_version, mirror_pct, min_delta, alpha,
                    min_samples, auto_promote, enabled, updated_at, updated_by)
               VALUES (?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP, ?)""",
            (
                model,
                tenant,
                challenger_version,
                mirror_pct,
                min_delta,
                alpha,
                min_samples,
                1 if auto_promote else 0,
                1 if enabled else 0,
                updated_by,
            ),
        )


# ── LLM/VLM serving endpoints (Track V, ADR 0107) ─────────────────────────────
#
# The `llm_endpoints` table pre-dated any writer: it was created for the F10 LLMOps
# console, which read it and always found it empty. These helpers are that missing writer,
# so `exa serve llm` and the dashboard now share one registry of what is actually running.


def upsert_llm_endpoint(
    model: str,
    *,
    hf_model_id: str,
    engine: str = "vllm",
    base_url: str | None = None,
    state: str = "PENDING",
    launcher: str = "external",
    job_id: str | None = None,
    cluster: str | None = None,
    project: str | None = None,
    modality: str = "text",
    served_model_name: str | None = None,
    engine_config: dict[str, Any] | None = None,
    max_model_len: int | None = None,
    tensor_parallel_size: int = 1,
    dtype: str = "auto",
    gpus: int | None = None,
    nodes: int | None = None,
    enabled: bool = True,
    updated_by: str | None = None,
) -> None:
    """Record (or replace) an endpoint. ``model`` is the primary key — one endpoint per model."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO llm_endpoints
                   (model, engine, hf_model_id, max_model_len, tensor_parallel_size, dtype,
                    enabled, updated_at, updated_by, base_url, state, launcher, job_id,
                    cluster, project, modality, served_model_name, engine_config, gpus,
                    nodes, created_at)
               VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP, ?,?,?,?,?,?,?,?,?,?,?,?,
                       COALESCE((SELECT created_at FROM llm_endpoints WHERE model = ?),
                                CURRENT_TIMESTAMP))
               ON CONFLICT(model) DO UPDATE SET
                    engine=excluded.engine, hf_model_id=excluded.hf_model_id,
                    max_model_len=excluded.max_model_len,
                    tensor_parallel_size=excluded.tensor_parallel_size,
                    dtype=excluded.dtype, enabled=excluded.enabled,
                    updated_at=CURRENT_TIMESTAMP, updated_by=excluded.updated_by,
                    base_url=excluded.base_url, state=excluded.state,
                    launcher=excluded.launcher, job_id=excluded.job_id,
                    cluster=excluded.cluster, project=excluded.project,
                    modality=excluded.modality,
                    served_model_name=excluded.served_model_name,
                    engine_config=excluded.engine_config, gpus=excluded.gpus,
                    nodes=excluded.nodes""",
            (
                model,
                engine,
                hf_model_id,
                max_model_len,
                tensor_parallel_size,
                dtype,
                1 if enabled else 0,
                updated_by,
                base_url,
                state,
                launcher,
                job_id,
                cluster,
                project,
                modality,
                served_model_name,
                json.dumps(engine_config) if engine_config is not None else None,
                gpus,
                nodes,
                model,
            ),
        )


def set_llm_endpoint_state(
    model: str, state: str, *, base_url: str | None = None, last_health: str | None = None
) -> None:
    """Move an endpoint's lifecycle state; optionally record the resolved URL / health note."""
    init_db()
    sets = ["state = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [state]
    if base_url is not None:
        sets.append("base_url = ?")
        params.append(base_url)
    if last_health is not None:
        sets.append("last_health = ?")
        params.append(last_health)
    params.append(model)
    with get_db() as conn:
        conn.execute(f"UPDATE llm_endpoints SET {', '.join(sets)} WHERE model = ?", params)


def get_llm_endpoint(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM llm_endpoints WHERE model = ?", (model,)).fetchone()
    return _endpoint_row(row) if row else None


def list_llm_endpoints(
    *, project: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    init_db()
    sql = "SELECT * FROM llm_endpoints"
    where, params = [], []
    if project:
        where.append("project = ?")
        params.append(project)
    if state:
        where.append("state = ?")
        params.append(state)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY model"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_endpoint_row(r) for r in rows]


def delete_llm_endpoint(model: str) -> bool:
    init_db()
    with get_db() as conn:
        cur = conn.execute("DELETE FROM llm_endpoints WHERE model = ?", (model,))
        return cur.rowcount > 0


def _endpoint_row(row: Any) -> dict[str, Any]:
    rec = dict(row)
    raw = rec.get("engine_config")
    if raw:
        try:
            rec["engine_config"] = json.loads(raw)
        except (TypeError, ValueError):
            rec["engine_config"] = None
    rec["enabled"] = bool(rec.get("enabled", 1))
    return rec


install_write_retry(__name__)
