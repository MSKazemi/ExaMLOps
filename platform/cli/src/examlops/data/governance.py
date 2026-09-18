"""examlops.data.governance — Governance — authz/policy/SLO/compliance.

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
    "get_compliance_system",
    "get_fairness_config",
    "get_fairness_samples",
    "get_policy_bundle",
    "get_relations_for",
    "get_slo_spec",
    "grant_relation",
    "list_compliance_systems",
    "list_objects_for",
    "list_policy_bundles",
    "list_relations",
    "list_slo_specs",
    "ANNEX_IV",
    "DECLARATION",
    "list_technical_files",
    "record_fairness_sample",
    "record_slo_sample",
    "revoke_relation",
    "revoke_virtual_key",
    "save_technical_file",
    "set_compliance_system",
    "set_fairness_config",
    "slo_sli_ratio",
    "store_policy_bundle",
    "upsert_slo_spec",
]

#: The Annex-IV technical file. Rows written before ``kind`` existed carry NULL and are this.
ANNEX_IV = "annex_iv"
#: The Annex-V EU Declaration of Conformity (ADR 0012 clause 4).
DECLARATION = "declaration"


def get_compliance_system(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM compliance_systems WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def get_fairness_config(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM fairness_config WHERE model=?", (model,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["slice_attrs"] = json.loads(d["slice_attrs"]) if d["slice_attrs"] else []
    return d


def get_fairness_samples(
    model: str, slice_attr: str, *, tenant: str = "default", last_n: int = 20000
) -> list[dict[str, Any]]:
    """Recent fairness samples for one slicing attribute."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT slice_value, prediction, label FROM fairness_samples
               WHERE model=? AND tenant=? AND slice_attr=? ORDER BY id DESC LIMIT ?""",
            (model, tenant, slice_attr, last_n),
        ).fetchall()
    return [dict(r) for r in rows]


def get_policy_bundle(tenant: str = "default", version: int | None = None) -> dict[str, Any] | None:
    """Return a policy bundle (latest for the tenant, or a specific version)."""
    init_db()
    with get_db() as conn:
        if version is not None:
            row = conn.execute(
                "SELECT * FROM policy_bundles WHERE tenant=? AND version=?", (tenant, version)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM policy_bundles WHERE tenant=? ORDER BY version DESC LIMIT 1",
                (tenant,),
            ).fetchone()
    return dict(row) if row else None


def get_relations_for(subject: str, obj: str) -> list[str]:
    """Return the relations ``subject`` holds directly on ``obj``."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT relation FROM authz_relations WHERE subject=? AND object=?", (subject, obj)
        ).fetchall()
    return [r["relation"] for r in rows]


def get_slo_spec(model: str, name: str, tenant: str = "default") -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM slo_specs WHERE model=? AND tenant=? AND name=?",
            (model, tenant, name),
        ).fetchone()
    return dict(row) if row else None


def grant_relation(subject: str, relation: str, obj: str, *, actor: str | None = None) -> None:
    """Grant ``subject`` a ``relation`` on ``obj`` (idempotent)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO authz_relations (subject, relation, object, actor)
               VALUES (?,?,?,?)
               ON CONFLICT(subject, relation, object) DO NOTHING""",
            (subject, relation, obj, actor),
        )


def list_compliance_systems(*, tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    where = "WHERE tenant=?" if tenant else ""
    params = (tenant,) if tenant else ()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM compliance_systems {where} ORDER BY model", params
        ).fetchall()
    return [dict(r) for r in rows]


def list_objects_for(subject: str) -> list[dict[str, Any]]:
    """All (object, relation) pairs granted to ``subject``."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT object, relation FROM authz_relations WHERE subject=? ORDER BY object",
            (subject,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_policy_bundles(tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        if tenant:
            rows = conn.execute(
                "SELECT * FROM policy_bundles WHERE tenant=? ORDER BY version DESC", (tenant,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM policy_bundles ORDER BY tenant, version DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def list_relations(subject: str | None = None, obj: str | None = None) -> list[dict[str, Any]]:
    init_db()
    clauses, params = [], []
    if subject:
        clauses.append("subject=?")
        params.append(subject)
    if obj:
        clauses.append("object=?")
        params.append(obj)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM authz_relations{where} ORDER BY object, subject", params
        ).fetchall()
    return [dict(r) for r in rows]


def list_slo_specs(*, model: str | None = None, tenant: str | None = None) -> list[dict[str, Any]]:
    """All SLO specs, optionally filtered by model/tenant (R5)."""
    init_db()
    clauses, params = [], []
    if model:
        clauses.append("model=?")
        params.append(model)
    if tenant:
        clauses.append("tenant=?")
        params.append(tenant)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM slo_specs {where} ORDER BY model, name", tuple(params)
        ).fetchall()
    return [dict(r) for r in rows]


def list_technical_files(model: str, *, kind: str = ANNEX_IV) -> list[dict[str, Any]]:
    """Versions of one compliance document kind, newest first.

    Defaults to the Annex-IV technical file, so every caller written before the Declaration of
    Conformity existed keeps listing exactly what it listed.
    """
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT version, gaps, generated_at, generated_by FROM technical_files "
            "WHERE model=? AND COALESCE(kind, ?)=? ORDER BY version DESC",
            (model, ANNEX_IV, kind),
        ).fetchall()
    return [dict(r) for r in rows]


def record_fairness_sample(
    model: str,
    slice_attr: str,
    slice_value: str,
    *,
    tenant: str = "default",
    prediction: float | None = None,
    label: float | None = None,
) -> None:
    """Log one per-request fairness sample (R2)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO fairness_samples
                   (model, tenant, slice_attr, slice_value, prediction, label)
               VALUES (?,?,?,?,?,?)""",
            (model, tenant, slice_attr, slice_value, prediction, label),
        )


def record_slo_sample(
    model: str,
    name: str,
    good: float,
    total: float,
    *,
    tenant: str = "default",
    watermark: str | None = None,
) -> None:
    """Append one SLI good/total measurement interval (R4).

    ``watermark`` is the high-water mark of the events this interval counted (``<table>:<id>``),
    set by the ingesters so the next ingest counts only newer events.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO slo_samples (model, tenant, name, good, total, watermark) "
            "VALUES (?,?,?,?,?,?)",
            (model, tenant, name, good, total, watermark),
        )


# Not in `__all__` (the facade contract: every name there is `platform_db`'s own); read only by
# `examlops.slo`'s incremental ingesters.
def slo_last_watermark(model: str, name: str, *, tenant: str = "default") -> str | None:
    """The newest ingest watermark recorded for an SLO, or None if none was ever recorded."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT watermark FROM slo_samples WHERE model=? AND tenant=? AND name=? "
            "AND watermark IS NOT NULL ORDER BY id DESC LIMIT 1",
            (model, tenant, name),
        ).fetchone()
    return str(row[0]) if row else None


def revoke_relation(subject: str, relation: str, obj: str) -> int:
    """Revoke a relation. Returns rows deleted (0 if none)."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM authz_relations WHERE subject=? AND relation=? AND object=?",
            (subject, relation, obj),
        )
        return cur.rowcount


def revoke_virtual_key(key_hash: str) -> None:
    init_db()
    with get_db() as conn:
        conn.execute("UPDATE virtual_keys SET revoked=1 WHERE key_hash=?", (key_hash,))


def save_technical_file(
    model: str,
    content: str,
    *,
    tenant: str = "default",
    gaps: int = 0,
    generated_by: str | None = None,
    kind: str = ANNEX_IV,
) -> int:
    """Persist a new (versioned) compliance document; returns the new version (R5).

    **Versions run per (model, kind).** One sequence shared across document kinds would make
    "technical file v4" and "declaration v5" describe the same model at the same moment with
    numbers that suggest one is newer than the other. The NULL-is-Annex-IV read keeps every
    pre-existing row in its own sequence, so no technical file changes version.
    """
    init_db()
    with get_db() as conn:
        prev = conn.execute(
            "SELECT MAX(version) AS v FROM technical_files WHERE model=? AND COALESCE(kind, ?)=?",
            (model, ANNEX_IV, kind),
        ).fetchone()
        version = (prev["v"] or 0) + 1
        conn.execute(
            """INSERT INTO technical_files
                   (model, tenant, version, gaps, content, generated_by, kind)
               VALUES (?,?,?,?,?,?,?)""",
            (model, tenant, version, gaps, content, generated_by, kind),
        )
    return version


def set_compliance_system(
    model: str,
    *,
    tenant: str = "default",
    in_scope: bool = True,
    risk_tier: str | None = None,
    intended_purpose: str | None = None,
    deployment_context: str | None = None,
    conformity_state: str | None = None,
    updated_by: str | None = None,
) -> None:
    """Upsert a system's compliance record (classification / conformity) (R1)."""
    init_db()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM compliance_systems WHERE model=?", (model,)
        ).fetchone()
        if existing:
            cur = dict(existing)
            conn.execute(
                """UPDATE compliance_systems SET tenant=?, in_scope=?, risk_tier=?,
                       intended_purpose=?, deployment_context=?, conformity_state=?,
                       updated_at=CURRENT_TIMESTAMP, updated_by=? WHERE model=?""",
                (
                    tenant,
                    1 if in_scope else 0,
                    risk_tier if risk_tier is not None else cur["risk_tier"],
                    intended_purpose if intended_purpose is not None else cur["intended_purpose"],
                    deployment_context
                    if deployment_context is not None
                    else cur["deployment_context"],
                    conformity_state if conformity_state is not None else cur["conformity_state"],
                    updated_by,
                    model,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO compliance_systems
                       (model, tenant, in_scope, risk_tier, intended_purpose,
                        deployment_context, conformity_state, updated_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    model,
                    tenant,
                    1 if in_scope else 0,
                    risk_tier,
                    intended_purpose,
                    deployment_context,
                    conformity_state or "draft",
                    updated_by,
                ),
            )


def set_fairness_config(
    model: str,
    slice_attrs: list[str],
    *,
    tenant: str = "default",
    threshold: float = 0.1,
    min_samples: int = 30,
    gate_promotion: bool = False,
    enabled: bool = True,
) -> None:
    """Declare slicing attributes + disparity threshold for a model (R1)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fairness_config
                   (model, tenant, slice_attrs, threshold, min_samples,
                    gate_promotion, enabled, updated_at)
               VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP)""",
            (
                model,
                tenant,
                json.dumps(slice_attrs),
                threshold,
                min_samples,
                1 if gate_promotion else 0,
                1 if enabled else 0,
            ),
        )


def slo_sli_ratio(
    model: str,
    name: str,
    *,
    tenant: str = "default",
    since: str | None = None,
    last_n: int = 1000,
) -> tuple[float, float]:
    """Aggregate (good, total) for an SLO over the samples inside its window.

    ``since`` is the start of the SLO's rolling window (ADR 0023: "a target for an SLI over a
    window"). Without it the sum is over the most recent ``last_n`` samples whatever their age,
    which is a count, not a window — a 30-day SLO then measured the last thousand samples, an hour
    on a busy service and half a year on a quiet one.

    ``last_n`` stays as a bound on how much is read, not as the definition of the window.
    """
    init_db()
    clause = " AND ts >= ?" if since else ""
    params: tuple = (model, tenant, name) + ((since,) if since else ()) + (last_n,)
    with get_db() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(good),0) AS good, COALESCE(SUM(total),0) AS total
               FROM (SELECT good, total FROM slo_samples
                     WHERE model=? AND tenant=? AND name=?{clause}
                     ORDER BY id DESC LIMIT ?)""",
            params,
        ).fetchone()
    return float(row["good"]), float(row["total"])


def store_policy_bundle(
    tenant: str,
    content: str,
    content_hash: str,
    *,
    signature: str | None = None,
    algo: str | None = None,
    signed_by: str | None = None,
) -> int:
    """Persist a signed policy bundle version for a tenant (R2). Returns the version."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS mx FROM policy_bundles WHERE tenant=?", (tenant,)
        ).fetchone()
        version = (row["mx"] or 0) + 1
        conn.execute(
            """INSERT INTO policy_bundles
                   (tenant, version, content_hash, content, signature, algo, signed_by)
               VALUES (?,?,?,?,?,?,?)""",
            (tenant, version, content_hash, content, signature, algo, signed_by),
        )
    return version


def upsert_slo_spec(
    model: str,
    name: str,
    *,
    tenant: str = "default",
    sli_source: str = "prometheus",
    sli_query: str | None = None,
    target: float = 0.99,
    window: str = "30d",
    higher_is_better: bool = True,
    gate_promotion: bool = False,
) -> None:
    """Insert or version-bump an SLO spec (R1/R7 — versioned + per-tenant)."""
    init_db()
    with get_db() as conn:
        prev = conn.execute(
            "SELECT version FROM slo_specs WHERE model=? AND tenant=? AND name=?",
            (model, tenant, name),
        ).fetchone()
        version = (prev["version"] + 1) if prev else 1
        conn.execute(
            """INSERT INTO slo_specs
                   (model, tenant, name, sli_source, sli_query, target, window,
                    higher_is_better, version, gate_promotion, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(model, tenant, name) DO UPDATE SET
                    sli_source=excluded.sli_source, sli_query=excluded.sli_query,
                    target=excluded.target, window=excluded.window,
                    higher_is_better=excluded.higher_is_better,
                    version=excluded.version, gate_promotion=excluded.gate_promotion,
                    updated_at=CURRENT_TIMESTAMP""",
            (
                model,
                tenant,
                name,
                sli_source,
                sli_query,
                target,
                window,
                1 if higher_is_better else 0,
                version,
                1 if gate_promotion else 0,
            ),
        )


install_write_retry(__name__)
