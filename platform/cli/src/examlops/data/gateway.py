"""examlops.data.gateway — LLM gateway — routing/cache/RAG/guardrails/vectors.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "cache_stats",
    "create_virtual_key",
    "get_gateway_config",
    "get_virtual_key",
    "list_virtual_keys",
    "record_gateway_call",
    "set_gateway_config",
]


def cache_stats(tenant: str | None = None) -> dict[str, Any]:
    """Aggregate hit-rate + measured savings (R7) — for the dashboard caching panel."""
    init_db()
    where = "WHERE tenant=?" if tenant else ""
    params = (tenant,) if tenant else ()
    with get_db() as conn:
        row = conn.execute(
            f"""SELECT
                    COALESCE(SUM(hit),0)              AS hits,
                    COALESCE(SUM(1-hit),0)            AS misses,
                    COUNT(*)                          AS total,
                    COALESCE(SUM(tokens_saved),0)     AS tokens_saved,
                    COALESCE(SUM(cost_saved),0)       AS cost_saved
                FROM cache_events {where}""",
            params,
        ).fetchone()
    hits, total = int(row["hits"]), int(row["total"])
    hit_rate = (hits / total) if total else 0.0
    cost_saved = float(row["cost_saved"])
    # ADR 0083: swap the formula, not just the coefficients (opt-in — see cache_savings_via_provider).
    try:
        from examlops.llmops_providers import cache_savings_via_provider

        via_provider = cache_savings_via_provider(
            total_calls=total, cache_hits=hits, cost_saved_usd=cost_saved
        )
    except Exception:
        via_provider = None
    if via_provider is not None:
        hit_rate, cost_saved = via_provider["hit_rate"], via_provider["cost_saved_usd"]
    return {
        "hits": hits,
        "misses": int(row["misses"]),
        "total": total,
        "hit_rate": hit_rate,
        "tokens_saved": int(row["tokens_saved"]),
        "cost_saved": cost_saved,
    }


def create_virtual_key(
    key_hash: str,
    *,
    tenant: str = "default",
    project: str = "default",
    models: list[str] | None = None,
    budget_usd: float | None = None,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    created_by: str | None = None,
) -> None:
    """Store a virtual key (only its hash — never the raw key).

    ``rpm_limit``/``tpm_limit`` (BL-107) are requests-per-minute / tokens-per-minute caps, ``None``
    = unlimited — same convention as ``budget_usd``. Enforced by the gateway service via the shared
    ``Coordinator``, not here; this function only persists the configured limit.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO virtual_keys
                   (key_hash, tenant, project, models_json, budget_usd, rpm_limit, tpm_limit,
                    spent_usd, created_by)
               VALUES (?,?,?,?,?,?,?,
                   COALESCE((SELECT spent_usd FROM virtual_keys WHERE key_hash=?), 0), ?)""",
            (
                key_hash,
                tenant,
                project,
                json.dumps(models or []),
                budget_usd,
                rpm_limit,
                tpm_limit,
                key_hash,
                created_by,
            ),
        )


def get_gateway_config(model: str, tenant: str = "default") -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM inference_gateway_config WHERE model=? AND tenant=?", (model, tenant)
        ).fetchone()
    return dict(row) if row else None


def get_virtual_key(key_hash: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM virtual_keys WHERE key_hash=?", (key_hash,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["models"] = json.loads(d.pop("models_json"))
    return d


def list_virtual_keys() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM virtual_keys ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["models"] = json.loads(d.pop("models_json"))
        out.append(d)
    return out


def record_gateway_call(
    key_hash: str | None,
    model: str,
    *,
    backend: str | None = None,
    cost_usd: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float | None = None,
    error: bool = False,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO gateway_calls
                   (key_hash, model, backend, cost_usd, prompt_tokens, completion_tokens,
                    latency_ms, error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                key_hash,
                model,
                backend,
                cost_usd,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                1 if error else 0,
            ),
        )


# Not in `__all__`: the facade contract is that every name there is `platform_db`'s own, and this
# is read only by `examlops.slo` (ADR 0023 clause 3, the `c1` source).
def gateway_call_sli(
    model: str, since: str, *, latency_ms_max: float | None = None, after_id: int = 0
) -> tuple[int, int, int]:
    """``(good, total, last_id)`` over a model's **measured** gateway calls since ``since``
    (UTC) and with ``id > after_id`` — the incremental ingester's watermark.

    Only rows that carry a measurement count — rows written before latency was recorded have
    ``latency_ms`` NULL and are *unmeasured*, not fast. With ``latency_ms_max`` it is a latency
    SLI over **successful** calls (a failed call is the error SLI's business, the usual SRE
    split); without it, an error SLI: good = calls that did not fail.
    """
    init_db()
    with get_db() as conn:
        if latency_ms_max is None:
            row = conn.execute(
                "SELECT SUM(CASE WHEN error = 0 THEN 1 ELSE 0 END), COUNT(*), MAX(id) "
                "FROM gateway_calls "
                "WHERE model = ? AND ts >= ? AND id > ? AND latency_ms IS NOT NULL",
                (model, since, after_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT SUM(CASE WHEN latency_ms <= ? THEN 1 ELSE 0 END), COUNT(*), MAX(id) "
                "FROM gateway_calls "
                "WHERE model = ? AND ts >= ? AND id > ? AND latency_ms IS NOT NULL AND error = 0",
                (latency_ms_max, model, since, after_id),
            ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or after_id)


def set_gateway_config(
    model: str,
    *,
    tenant: str = "default",
    mode: str = "round_robin",
    slo_latency_ms: float | None = None,
    disaggregate: bool = False,
    prefill_pool: str | None = None,
    decode_pool: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO inference_gateway_config
                   (model, tenant, mode, slo_latency_ms, disaggregate, prefill_pool,
                    decode_pool, updated_at)
               VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(model, tenant) DO UPDATE SET
                   mode=excluded.mode, slo_latency_ms=excluded.slo_latency_ms,
                   disaggregate=excluded.disaggregate, prefill_pool=excluded.prefill_pool,
                   decode_pool=excluded.decode_pool, updated_at=CURRENT_TIMESTAMP""",
            (
                model,
                tenant,
                mode,
                slo_latency_ms,
                1 if disaggregate else 0,
                prefill_pool,
                decode_pool,
            ),
        )


install_write_retry(__name__)
