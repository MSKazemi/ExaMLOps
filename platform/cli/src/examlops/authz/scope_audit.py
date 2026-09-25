"""Static tenant/project scope audit of the ``platform.db`` schema (ADR 0014 decision 3).

Decision 3 says every ``platform_db`` object belongs to a project. That is a claim about the *whole*
schema, and it decays silently: a new table is one ``CREATE TABLE`` away from being cross-tenant
data. This module reads the live schema (``sqlite_master`` + ``table_info`` - read-only, no rows
are touched) and classifies every table:

* ``scoped``        - carries a ``tenant`` or ``project`` column: rows are partitioned directly.
* ``model-scoped``  - keyed by ``model``; a model belongs to a project through ``project_resources``
                      / ``project_models``, so the project is reachable but one join away.
* ``dataset-scoped`` - keyed by ``dataset``; a dataset belongs to a project through
                      ``project_resources`` (kind ``dataset``), and the ``exa data`` revision
                      commands enforce it (:func:`examlops.authz.guard.resource_allowed`) - an
                      unassigned dataset is the ``default`` project's (ADR 0014 decision 5).
* ``exempt``        - listed in :data:`EXEMPT` with a *kind* and a *reason*:
    ``global``   platform infrastructure, not tenant data (coordination locks, schema meta ...);
    ``security`` the enforcement store itself (grants, principals, audit checkpoints);
    ``root``     the scope roots themselves (``projects``);
    ``child``    rows reachable only through a scoped parent id;
    ``gap``      **user data that is NOT partitioned by project today** - a known, named gap, kept
                 in the list so the report is honest and so removing an entry (by adding the
                 column) is a visible, ratcheted step.
* ``UNSCOPED``      - none of the above. A new table lands here until someone decides, and the
                      guard test (``tests/unit/test_scope_audit.py``) fails the build on it.

An exemption whose table no longer exists, or has since gained a scope column, is reported as
``stale`` and also fails the guard: the list can only shrink toward the truth.
"""

from __future__ import annotations

from typing import Any

# kind -> {table: reason}
_GLOBAL = "platform infrastructure (not tenant data)"
_SECURITY = "the authorization / integrity store itself"
_CHILD = "reachable only through its scoped parent's id"
_GAP = "user data keyed by name; project ownership is not recorded (known gap)"

EXEMPT: dict[str, tuple[str, str]] = {
    **{
        t: ("global", _GLOBAL)
        for t in (
            "autopilot_config",
            "autopilot_lease",
            "autopilot_runs",
            "burst_events",
            # ADR 0158: the model catalog is a curated, platform-wide list of what one COULD
            # start from — deliberately not partitioned by project. A project only appears once
            # an entry is pulled, and `catalog_pulls` carries that `project` column itself.
            "catalog_entries",
            "coord_idempotency",
            "coord_locks",
            "coord_rate",
            "device_pools",
            "encoders",
            "event_inbox",
            "hpc_clusters",
            "hpc_nodes",
            "idempotency_keys",
            "placement_decisions",
            "platform_meta",
            "platform_upgrades",
            "serving_snapshots",
        )
    },
    **{
        t: ("security", _SECURITY)
        for t in (
            "agent_plans",
            "agent_principals",
            "audit_checkpoints",
            "audit_maintenance_runs",
            "audit_transparency_entries",
            "authz_relations",
            "identity_grants",
            "identity_leases",
        )
    },
    "projects": ("root", "the project registry: `name` IS the scope key"),
    "namespaces": ("root", "the legacy namespace registry (superseded by projects)"),
    **{
        t: ("child", _CHILD)
        for t in (
            "ab_assignments",
            "ab_results",
            "asset_materializations",
            "canary_steps",
            "eval_results",
            "federated_rounds",
            "federated_sites",
            "feature_view_materializations",
            "hpo_trials",
            "lineage_io",
        )
    },
    **{
        t: ("gap", _GAP)
        for t in (
            "agent_alias_history",
            "agent_aliases",
            "agent_reeval_queue",
            "agent_rollouts",
            "agent_versions",
            "assets",
            "feature_records",
            "feature_views",
            "federated_runs",
            "genai_app_alias_history",
            "genai_app_aliases",
            "genai_applications",
            "ground_truth",
            "hardware_profile_labels",
            "hardware_profile_versions",
            "judge_calibrations",
            "lora_adapters",
            "online_features",
            "prompt_label_splits",
            "prompt_labels",
            "prompt_versions",
            "tool_call_counters",
            "tool_grants",
            "training_checkpoints",
        )
    },
}

_SCOPE_COLUMNS = ("tenant", "project")


class ScopeAuditUnavailable(RuntimeError):
    """The datastore cannot be introspected this way (e.g. a non-SQLite backend)."""


def _schema() -> dict[str, list[str]]:
    from examlops.data import get_db, init_db

    init_db()
    try:
        with get_db() as conn:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE ? "
                    "ORDER BY name",
                    ("sqlite_%",),
                ).fetchall()
            ]
            return {
                n: [r["name"] for r in conn.execute(f"PRAGMA table_info({n})").fetchall()]
                for n in names
            }
    except Exception as exc:  # noqa: BLE001 - reported, not guessed
        raise ScopeAuditUnavailable(f"cannot read the schema: {type(exc).__name__}: {exc}") from exc


def classify(table: str, columns: list[str]) -> tuple[str, str]:
    """``(status, detail)`` for one table."""
    for col in _SCOPE_COLUMNS:
        if col in columns:
            return "scoped", f"column `{col}`"
    if "model" in columns:
        return "model-scoped", "via model -> project_resources"
    if "dataset" in columns:
        return "dataset-scoped", "via dataset -> project_resources(kind='dataset')"
    if table in EXEMPT:
        kind, reason = EXEMPT[table]
        return "exempt", f"{kind}: {reason}"
    return "UNSCOPED", "no tenant/project/model column and not in EXEMPT"


def audit_scope(schema: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Classify every table. Pure given ``schema``; reads the live schema otherwise."""
    schema = schema if schema is not None else _schema()
    tables = []
    for name in sorted(schema):
        status, detail = classify(name, schema[name])
        tables.append({"table": name, "status": status, "detail": detail})
    stale = sorted(
        t
        for t, _ in EXEMPT.items()
        if t not in schema or any(c in schema[t] for c in (*_SCOPE_COLUMNS, "model", "dataset"))
    )
    by_status: dict[str, int] = {}
    for t in tables:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    gaps = sorted(t for t, (k, _) in EXEMPT.items() if k == "gap" and t in schema)
    return {
        "ok": not by_status.get("UNSCOPED") and not stale,
        "total": len(tables),
        "summary": by_status,
        "unscoped": [t["table"] for t in tables if t["status"] == "UNSCOPED"],
        "stale_exemptions": stale,
        "known_gaps": gaps,
        "tables": tables,
    }
