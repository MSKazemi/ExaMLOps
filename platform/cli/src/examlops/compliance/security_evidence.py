"""Authorization (D6), secrets (D7) and environmental (FinOps) evidence collectors (ADR 0027).

ADR 0027 decision 3 lists D6 and D7 among the sources ``exa governance report`` must pull live
evidence from. Before these collectors existed, the RBAC control was "satisfied" by audit-trail
coverage alone — a record that events were logged, not that anyone's access was documented or
enforced. Each collector here answers one narrow question and says, in its content, what it did
*not* check:

``access_documented``  (D6) does someone **own** the system? At least one ``owner`` relation in
                        ``authz_relations`` must cover the model — directly (``model:<m>``,
                        ``…/model:<m>``) or through a project the model is assigned to. Viewer or
                        editor grants without an owner are reported and are not enough: an AI
                        system nobody is accountable for is the gap GOVERN 2.1 describes.
``access_enforced``    (D6) is authorization actually **checked**? D6's ``check()`` allows
                        everything unless ``EXAMLOPS_MULTITENANCY`` is on, so relations on their
                        own prove nothing about enforcement. Read from the environment of the
                        process producing the report — the content says so, and the section is
                        never *verified* (runtime configuration is outside the hash chain).
``secrets_managed``    (D7) are the tenant's credentials held in a managed store (OpenBao, or the
                        Fernet-encrypted local store) rather than only in plain environment
                        variables?
``secrets_rotation``   (D7) has every stored secret been written or rotated within the rotation
                        window (``EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS``, default 90)? A store
                        held entirely in OpenBao is reported as not visible — a gap, not a pass.
``environmental_impact`` (FinOps / Green-AI) has the model's training energy or carbon been
                        recorded (``carbon_records``)?

Every collector returns ``(present, content)`` like the rest of
:data:`examlops.compliance._COLLECTORS`, never raises (a failed read is a gap, with the error in
the content), and bounds its work to aggregate counts — no unbounded row listing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from examlops import data as platform_db

#: Default rotation window for :func:`ev_secrets_rotation`, in days.
DEFAULT_SECRET_MAX_AGE_DAYS = 90
#: Upper bound on the configurable window: a "rotation policy" of decades is no policy.
_MAX_SECRET_MAX_AGE_DAYS = 3650

_TRUTHY = {"1", "true", "yes", "on"}


def secret_max_age_days() -> int:
    """The rotation window. A malformed or out-of-range value falls back to the default.

    Falling back rather than raising keeps the report available; the content of
    :func:`ev_secrets_rotation` names the window it actually applied, so a typo cannot silently
    loosen the policy unnoticed.
    """
    raw = os.getenv("EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS", "").strip()
    if not raw:
        return DEFAULT_SECRET_MAX_AGE_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SECRET_MAX_AGE_DAYS
    if value < 1 or value > _MAX_SECRET_MAX_AGE_DAYS:
        return DEFAULT_SECRET_MAX_AGE_DAYS
    return value


def _cutoff(days: int) -> str:
    # `CURRENT_TIMESTAMP` stores UTC as 'YYYY-MM-DD HH:MM:SS'; compare in the same shape.
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _model_projects(conn: Any, model: str) -> list[str]:
    """Projects the model is assigned to (ADR 0086 membership, both tables)."""
    projects: set[str] = set()
    for sql in (
        "SELECT project FROM project_models WHERE lower(model)=lower(?)",
        "SELECT project FROM project_resources WHERE kind='model' AND lower(ref)=lower(?)",
    ):
        try:
            for r in conn.execute(sql, (model,)).fetchall():
                if r["project"]:
                    projects.add(str(r["project"]))
        except Exception:  # noqa: BLE001 - a missing membership table means no membership
            continue
    return sorted(projects)


def _covering_where(model: str, projects: list[str], column: str) -> tuple[str, list[Any]]:
    """SQL predicate for objects that govern ``model`` (the model itself or its projects)."""
    m = model.lower()
    clauses = [f"lower({column})=?", f"lower({column}) LIKE ? ESCAPE '\\'"]
    params: list[Any] = [f"model:{m}", f"%/model:{_like_escape(m)}"]
    if projects:
        clauses.append(f"{column} IN ({','.join('?' for _ in projects)})")
        params.extend(f"project:{p}" for p in projects)
    return "(" + " OR ".join(clauses) + ")", params


def ev_access_documented(model: str, tenant: str) -> tuple[bool, str]:
    try:
        with platform_db.get_db() as conn:
            projects = _model_projects(conn, model)
            where, params = _covering_where(model, projects, "object")
            rows = conn.execute(
                f"SELECT relation, COUNT(*) AS c FROM authz_relations WHERE {where} "
                "GROUP BY relation",
                params,
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - a failed read is a gap, never a pass
        return False, f"Access relations could not be read (D6): {type(exc).__name__}: {exc}"
    counts = {str(r["relation"]): int(r["c"]) for r in rows}
    scope = f"model:{model}" + (f" + project(s) {', '.join(projects)}" if projects else "")
    if not counts:
        return False, (
            f"No roles are documented for {scope}: no authz relation covers it (D6). "
            "Grant one with: exa project add-member <project> <subject> --role owner"
        )
    listed = ", ".join(f"{n} {rel}" for rel, n in sorted(counts.items()))
    if not counts.get("owner"):
        return False, (
            f"Roles on {scope}: {listed} — but no owner. An AI system nobody is accountable for "
            "does not have its responsibilities documented (D6)."
        )
    return True, f"Documented roles on {scope}: {listed} (D6 relationship RBAC)."


def ev_access_enforced(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.authz import multitenancy_enabled
        from examlops.authz import openfga_client as _fga

        enforced = multitenancy_enabled()
        pdp = "OpenFGA" if _fga.config_from_env() is not None else "local relation store"
    except Exception as exc:  # noqa: BLE001
        return False, f"Authorization configuration could not be read (D6): {exc}"
    iam = bool(os.getenv("EXAMLOPS_IAM_CONFIG", "").strip())
    where_cfg = "as configured for the process that produced this report"
    if not enforced:
        return False, (
            "Authorization is NOT enforced: EXAMLOPS_MULTITENANCY is off, so every D6 check "
            f"allows (single-tenant mode), {where_cfg}. Documented roles are not enforced roles."
        )
    decisions: dict[str, int] = {}
    try:
        with platform_db.get_db() as conn:
            projects = _model_projects(conn, model)
            where, params = _covering_where(model, projects, "target")
            for r in conn.execute(
                "SELECT action, COUNT(*) AS c FROM audit_events WHERE action IN "
                f"('authz_deny','authz_error','authz_grant','authz_revoke') AND {where} "
                "GROUP BY action",
                params,
            ).fetchall():
                decisions[str(r["action"])] = int(r["c"])
    except Exception:  # noqa: BLE001 - decision counts are context, not the verdict
        decisions = {}
    recorded = ", ".join(f"{n} {a}" for a, n in sorted(decisions.items())) or "none yet"
    return True, (
        f"Authorization enforced, default-deny, {where_cfg}: EXAMLOPS_MULTITENANCY on, "
        f"decision point = {pdp}, federated identity trust file "
        f"{'configured' if iam else 'not configured'}. Audited authz decisions on this system: "
        f"{recorded} (D6)."
    )


def _vault_configured() -> bool:
    return bool(os.getenv("EXAMLOPS_VAULT_ADDR", "").strip())


def _store_counts(tenant: str) -> tuple[int, int, int]:
    """``(stored, legacy_unwrapped, older_than_window)`` for the tenant's local secrets."""
    cutoff = _cutoff(secret_max_age_days())
    with platform_db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN key_id IS NULL THEN 1 ELSE 0 END) AS legacy, "
            "SUM(CASE WHEN updated_at < ? THEN 1 ELSE 0 END) AS stale "
            "FROM secrets_store WHERE tenant=?",
            (cutoff, tenant),
        ).fetchone()
    return int(row["n"] or 0), int(row["legacy"] or 0), int(row["stale"] or 0)


def ev_secrets_managed(model: str, tenant: str) -> tuple[bool, str]:
    vault = _vault_configured()
    try:
        stored, legacy, _ = _store_counts(tenant)
    except Exception as exc:  # noqa: BLE001
        if vault:
            return True, (
                "Secrets backend: OpenBao/Vault configured (EXAMLOPS_VAULT_ADDR); the local "
                f"encrypted store could not be read ({type(exc).__name__}) (D7)."
            )
        return False, f"Secrets store could not be read (D7): {type(exc).__name__}: {exc}"
    if not vault and not stored:
        return False, (
            f"No managed secrets for tenant '{tenant}': no OpenBao/Vault is configured and the "
            "encrypted local store is empty, so credentials can only be coming from plain "
            "environment variables (D7)."
        )
    parts = []
    if vault:
        parts.append("OpenBao/Vault configured (EXAMLOPS_VAULT_ADDR)")
    if stored:
        parts.append(f"{stored} secret(s) Fernet-encrypted at rest in the local store")
    if legacy:
        parts.append(f"{legacy} of them carry no key id — run: exa secrets rewrap")
    return True, f"Managed secrets for tenant '{tenant}': " + "; ".join(parts) + " (D7)."


def ev_secrets_rotation(model: str, tenant: str) -> tuple[bool, str]:
    days = secret_max_age_days()
    try:
        stored, _, stale = _store_counts(tenant)
        cutoff = _cutoff(days)
        pattern = f'%"tenant": "{_like_escape(tenant)}"%'
        with platform_db.get_db() as conn:
            rotations = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM audit_events "
                    # A KEK rewrap re-encrypts the same value: it is not a credential rotation.
                    "WHERE action='secret_rotate' AND ts >= ? "
                    "AND (tenant=? OR details LIKE ? ESCAPE '\\')",
                    (cutoff, tenant, pattern),
                ).fetchone()["c"]
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"Secret rotation could not be assessed (D7): {type(exc).__name__}: {exc}"
    if not stored:
        if _vault_configured():
            return False, (
                f"Rotation is not visible: tenant '{tenant}' keeps its secrets in OpenBao/Vault, "
                "whose version history the platform does not read. Evidence it from the vault "
                "(D7)."
            )
        return False, f"No stored secrets for tenant '{tenant}' — nothing to rotate (D7)."
    if stale:
        return False, (
            f"{stale} of {stored} secret(s) for tenant '{tenant}' were last written more than "
            f"{days} day(s) ago — rotate with: exa secrets rotate <path> (D7). "
            f"{rotations} audited rotation(s) in the window."
        )
    return True, (
        f"All {stored} secret(s) for tenant '{tenant}' were written or rotated within the "
        f"{days}-day window; {rotations} audited rotation(s) in it (D7)."
    )


def ev_environmental_impact(model: str, tenant: str) -> tuple[bool, str]:
    try:
        with platform_db.get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, SUM(kwh) AS kwh, SUM(co2e_g) AS co2 "
                "FROM carbon_records WHERE lower(model)=lower(?)",
                (model,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return False, f"Carbon records could not be read (FinOps): {type(exc).__name__}: {exc}"
    n = int(row["n"] or 0)
    if not n:
        return False, (f"No energy / carbon accounting recorded for {model} (exa finops carbon).")
    kwh = float(row["kwh"] or 0.0)
    co2 = float(row["co2"] or 0.0)
    return True, (
        f"Environmental impact: {n} carbon record(s), {kwh:.3f} kWh, {co2:.1f} gCO2e "
        "(Green-AI accounting, exa finops carbon)."
    )


__all__ = [
    "DEFAULT_SECRET_MAX_AGE_DAYS",
    "ev_access_documented",
    "ev_access_enforced",
    "ev_environmental_impact",
    "ev_secrets_managed",
    "ev_secrets_rotation",
    "secret_max_age_days",
]
