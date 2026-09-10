"""Server-side feature-flag evaluator + staged rollout (F25 / ADR 0070).

Flags are evaluated **server-side** with context (tenant + role from F15 + a deterministic
percentage bucket) — the client receives *decisions*, not rules (R1/R2). Targeting supports
per-tenant / per-role / percentage staged rollout, deterministic per subject so a given user stays
on the same side of a rollout (R3/R5). Admin overrides persist in ``platform_db`` and are audited
(R4). Self-hosted, no third-party SaaS.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import audit_write
from dbconn import connect


@dataclass(frozen=True)
class FlagDef:
    name: str
    description: str
    default: bool
    # Targeting (all optional): restrict to tenants/roles; percentage staged rollout (0–100).
    tenants: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    percentage: int | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


# The flag registry. Frontend surfaces (F23 lib/flags.ts) mirror these names.
FLAG_DEFS: dict[str, FlagDef] = {
    "mlopsConsole": FlagDef("mlopsConsole", "F9 MLOps console", default=True),
    "facilityConsole": FlagDef("facilityConsole", "F6 exascale facility console", default=True),
    "commandPalette": FlagDef("commandPalette", "F2 ⌘K command palette", default=True),
    "llmopsConsole": FlagDef("llmopsConsole", "F10 LLMOps console", default=True),
    "projectsConsole": FlagDef(
        "projectsConsole", "Projects workspace console (ADR 0086)", default=True
    ),
    # Example staged rollout: admins always, everyone else at 50%.
    "incidentTimeline": FlagDef(
        "incidentTimeline",
        "F12 incident timeline (beta)",
        default=False,
        percentage=50,
        tags=("beta",),
    ),
}


# ── deterministic percentage bucket (F25 R5) ──────────────────────────────────


def subject_bucket(flag: str, subject: str) -> int:
    """Stable 0–99 bucket for (flag, subject) — same subject always lands the same (R5)."""
    digest = hashlib.sha256(f"{flag}:{subject}".encode()).hexdigest()
    return int(digest, 16) % 100


# ── evaluation (F25 R1/R3) ────────────────────────────────────────────────────


def evaluate(defn: FlagDef, *, role: str, tenant: str, subject: str, override: bool | None) -> bool:
    """Evaluate one flag for a context. ``override`` (admin on/off) wins over the default."""
    enabled = defn.default if override is None else override
    if not enabled:
        return False
    if defn.roles and role not in defn.roles:
        return False
    if defn.tenants and tenant not in defn.tenants:
        return False
    if defn.percentage is not None and defn.percentage < 100:
        # Admins bypass percentage gating so they can always reach a rollout for testing.
        if role != "admin":
            return subject_bucket(defn.name, subject) < defn.percentage
    return True


def _module_allows(name: str) -> bool:
    """False when the flag's module is switched off by the site profile (ADR 0128).

    The module gate sits *above* the flag: an admin override cannot turn on a console whose
    module this site does not run, because its API routes answer 404 regardless.
    """
    try:
        from module_gate import module_enabled

        from examlops.lifecycle.modules import module_for_flag
    except Exception:  # noqa: BLE001 — without the lifecycle package, flags behave as before
        return True
    return module_enabled(module_for_flag(name))


def evaluate_all(db_path: str, *, role: str, tenant: str, subject: str) -> dict[str, bool]:
    """Evaluate every flag for a context → the decisions the client consumes (R2)."""
    overrides = _load_overrides(db_path)
    return {
        name: _module_allows(name)
        and evaluate(defn, role=role, tenant=tenant, subject=subject, override=overrides.get(name))
        for name, defn in FLAG_DEFS.items()
    }


# ── admin: defs + overrides + set (F25 R4) ────────────────────────────────────


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feature_flag_overrides ("
        " name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " updated_by TEXT)"
    )


def _load_overrides(db_path: str) -> dict[str, bool]:
    try:
        conn = connect(db_path)
    except sqlite3.Error:  # pragma: no cover
        return {}
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature_flag_overrides'"
        ).fetchone():
            return {}
        return {
            r[0]: bool(r[1])
            for r in conn.execute("SELECT name, enabled FROM feature_flag_overrides")
        }
    finally:
        conn.close()


def admin_view(db_path: str) -> dict[str, Any]:
    """Flag definitions + current overrides + effective-default state, for the admin UI (R4)."""
    overrides = _load_overrides(db_path)
    flags = []
    for name, d in FLAG_DEFS.items():
        override = overrides.get(name)
        flags.append(
            {
                "name": d.name,
                "description": d.description,
                "default": d.default,
                "override": override,
                "effective": (d.default if override is None else override) and _module_allows(name),
                "module_enabled": _module_allows(name),
                "targeting": {
                    "tenants": list(d.tenants),
                    "roles": list(d.roles),
                    "percentage": d.percentage,
                },
                "tags": list(d.tags),
            }
        )
    return {"flags": flags, "count": len(flags)}


def set_override(db_path: str, name: str, enabled: bool, actor: str) -> bool:
    """Set an admin on/off override for a known flag; audited to ``audit_events`` (R4).

    Returns ``False`` for an unknown flag name (nothing written).
    """
    if name not in FLAG_DEFS:
        return False
    conn = connect(db_path)
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO feature_flag_overrides (name, enabled, updated_by) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, "
            "updated_at=CURRENT_TIMESTAMP, updated_by=excluded.updated_by",
            (name, int(enabled), actor),
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone():
            audit_write.audit(
                actor,
                "flag_set",
                name,
                {"enabled": bool(enabled)},
                source="dashboard-flags",
                conn=conn,
            )
        conn.commit()
        return True
    finally:
        conn.close()
