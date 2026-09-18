"""Traffic console (A/B testing + shadow deployments · Serve group).

Surfaces the `exa serve ab` and `exa serve shadow` CLI capabilities in the dashboard over pure
`platform.db` state — the same `examlops.data.get_db`/`init_db` code paths the CLI commands use
(no Ray, no httpx; the actual Ray traffic actuation is out of scope). This console manages the A/B
tests + recorded observations and the shadow config + recorded comparisons — exactly what the CLI
does.

Reads (viewer):
  * ``GET /v1/traffic/ab`` — A/B tests (mirrors ``exa serve ab status``) + a Welch/z analysis of the
    active test's recorded observations when the optional scientific stack is present (mirrors
    ``exa serve ab analyze``; ``examlops.analysis.ab_stats.analyze_ab``).
  * ``GET /v1/traffic/shadow`` — shadow config (``exa serve shadow status``) + last comparisons
    (``exa serve shadow log``).

Writes (admin + ``traffic.manage``, audited ``source=dashboard``):
  * ``POST /v1/traffic/ab/start`` — start an A/B test (mirrors ``exa serve ab start``); ``ab_test_started``.
  * ``POST /v1/traffic/ab/stop`` — stop the running A/B test (mirrors ``exa serve ab stop``); ``ab_test_stopped``.
  * ``POST /v1/traffic/shadow`` — enable/disable shadow (mirrors ``exa serve shadow enable``/``disable``);
    ``shadow_config_set``.

All reads fail open (never a 500); every mutation is audited ``source=dashboard``.
"""

from __future__ import annotations

import audit_write
from auth import require_role
from capabilities import TRAFFIC_MANAGE, can, deny_reason, require_capability
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/v1/traffic", tags=["traffic"])
_viewer = require_role("viewer")
_admin = require_role("admin")


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, TRAFFIC_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, TRAFFIC_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    """Append a plain ``source=dashboard`` audit row (same helper events.py/admission.py use)."""
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_data():
    """Lazy, guarded import of the shared platform.db code paths (503 if unavailable)."""
    try:
        from examlops.data import get_db, init_db  # type: ignore

        return get_db, init_db
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "traffic features require the examlops package (not available in this deployment)",
        ) from exc


# The A/B + shadow DDL is byte-identical to the CLI (`ab_cmd._AB_TABLES` / `shadow_cmd._ensure_tables`)
# so the dashboard and CLI share one schema on the same platform.db.
_AB_TABLES = """
CREATE TABLE IF NOT EXISTS ab_tests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model      TEXT NOT NULL,
    name       TEXT,
    variant_a  TEXT NOT NULL DEFAULT 'Production',
    variant_b  TEXT NOT NULL DEFAULT 'Canary',
    split_pct  INTEGER NOT NULL DEFAULT 50,
    status     TEXT NOT NULL DEFAULT 'running',
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at   DATETIME,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS ab_results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL,
    ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    variant TEXT NOT NULL,
    value   REAL NOT NULL
);
"""

_SHADOW_TABLES = """
CREATE TABLE IF NOT EXISTS shadow_config (
    model        TEXT PRIMARY KEY,
    shadow_alias TEXT NOT NULL DEFAULT 'Staging',
    enabled      INTEGER NOT NULL DEFAULT 1,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by   TEXT
);
CREATE TABLE IF NOT EXISTS shadow_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model           TEXT NOT NULL,
    production_pred REAL,
    shadow_pred     REAL,
    diff_pct        REAL,
    job_id          TEXT
);
"""


def _ensure_ab(conn) -> None:
    conn.executescript(_AB_TABLES)


def _ensure_shadow(conn) -> None:
    conn.executescript(_SHADOW_TABLES)


# ── A/B testing ───────────────────────────────────────────────────────────────


def _ab_analysis(conn, model: str) -> dict | None:
    """Welch/z analysis of the active A/B test's observations (mirrors ``exa serve ab analyze``).

    Returns ``None`` when there is no A/B test for the model or the optional scientific stack is
    absent — the console degrades gracefully (never a 500)."""
    try:
        from examlops.analysis.ab_stats import analyze_ab
    except ModuleNotFoundError:  # numpy/scipy are the optional `analysis` extra
        return None

    test_row = conn.execute(
        # `, id DESC`: two tests started for one model inside the same second tie on
        # `started_at`, and the loser's `variant_a`/`variant_b` would then select the samples this
        # analysis reports on — a statistical verdict about the wrong experiment.
        "SELECT id, variant_a, variant_b FROM ab_tests WHERE model=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (model,),
    ).fetchone()
    if test_row is None:
        return None
    test_id = test_row["id"]
    variant_a, variant_b = test_row["variant_a"], test_row["variant_b"]
    rows = conn.execute(
        "SELECT variant, value FROM ab_results WHERE test_id=?", (test_id,)
    ).fetchall()
    values_a = [r["value"] for r in rows if r["variant"] == variant_a]
    values_b = [r["value"] for r in rows if r["variant"] == variant_b]
    result = analyze_ab(values_a, values_b)
    return {
        "model": model,
        "test_id": test_id,
        "variant_a": variant_a,
        "variant_b": variant_b,
        **result,
    }


@router.get("/ab")
async def get_ab(model: str | None = None, _=Depends(_viewer)) -> dict:
    """A/B tests + a Welch/z analysis of the active test's recorded observations.

    Mirrors ``exa serve ab status`` (+ ``analyze``) via the shared ``examlops.data`` path. Fail-open."""
    get_db, init_db = _examlops_data()
    try:
        init_db()
        with get_db() as conn:
            _ensure_ab(conn)
            if model:
                rows = conn.execute(
                    "SELECT id, model, name, variant_a, variant_b, split_pct, status, started_at, "
                    "ended_at, created_by FROM ab_tests WHERE model=? ORDER BY started_at DESC LIMIT 50",
                    (model,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, model, name, variant_a, variant_b, split_pct, status, started_at, "
                    "ended_at, created_by FROM ab_tests ORDER BY started_at DESC LIMIT 50",
                ).fetchall()
            tests = [dict(r) for r in rows]
            analysis = _ab_analysis(conn, model) if model else None
        return {"tests": tests, "analysis": analysis}
    except Exception:
        return {"tests": [], "analysis": None}


@router.post("/ab/start")
async def ab_start(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Also through the enforcing dependency, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the admin dependency, never in place of it: `require_capability` admits
    # operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(TRAFFIC_MANAGE)),
) -> dict:
    """Start an A/B test (admin; audited ``source=dashboard``). Mirrors ``exa serve ab start``.

    Body: ``{model, variant_a?, variant_b?, split?, name?}``. 409 if one is already running."""
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")
    variant_a = (payload.get("variant_a") or "Production").strip() or "Production"
    variant_b = (payload.get("variant_b") or "Canary").strip() or "Canary"
    try:
        split = int(payload.get("split", 50))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "split must be an integer") from exc
    if not 0 <= split <= 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "split must be between 0 and 100")
    name = payload.get("name") or None

    get_db, init_db = _examlops_data()
    init_db()
    actor = principal.get("sub", "?")
    with get_db() as conn:
        _ensure_ab(conn)
        existing = conn.execute(
            "SELECT id FROM ab_tests WHERE model=? AND status='running'", (model,)
        ).fetchone()
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"An active A/B test already exists for {model} (id={existing['id']}). Stop it first.",
            )
        conn.execute(
            "INSERT INTO ab_tests (model, name, variant_a, variant_b, split_pct, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (model, name, variant_a, variant_b, split, actor),
        )
        _audit(
            conn,
            actor,
            "ab_test_started",
            model,
            {"variant_a": variant_a, "variant_b": variant_b, "split_pct": split, "name": name},
        )
    return {
        "model": model,
        "variant_a": variant_a,
        "variant_b": variant_b,
        "split_pct": split,
        "name": name,
    }


@router.post("/ab/stop")
async def ab_stop(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Also through the enforcing dependency, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the admin dependency, never in place of it: `require_capability` admits
    # operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(TRAFFIC_MANAGE)),
) -> dict:
    """Stop the running A/B test for a model (admin; audited). Mirrors ``exa serve ab stop``.

    Body: ``{model}``. Returns ``{stopped}`` — ``false`` when nothing was running."""
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")

    get_db, init_db = _examlops_data()
    init_db()
    actor = principal.get("sub", "?")
    with get_db() as conn:
        _ensure_ab(conn)
        cursor = conn.execute(
            "UPDATE ab_tests SET status='completed', ended_at=CURRENT_TIMESTAMP "
            "WHERE model=? AND status='running'",
            (model,),
        )
        affected = cursor.rowcount
        if affected:
            _audit(conn, actor, "ab_test_stopped", model, {})
    return {"model": model, "stopped": affected > 0}


# ── shadow deployments ────────────────────────────────────────────────────────


@router.get("/shadow")
async def get_shadow(model: str | None = None, _=Depends(_viewer)) -> dict:
    """Shadow config (``exa serve shadow status``) + last comparisons (``exa serve shadow log``).

    Fail-open: an unreachable/absent table returns empty lists (never a 500)."""
    get_db, init_db = _examlops_data()
    try:
        init_db()
        with get_db() as conn:
            _ensure_shadow(conn)
            if model:
                # Case-insensitive: serving stores the lowercase MLflow key (plan P0.4).
                key = model.strip().lower()
                config_rows = conn.execute(
                    "SELECT model, shadow_alias, enabled, updated_at, updated_by "
                    "FROM shadow_config WHERE lower(model)=?",
                    (key,),
                ).fetchall()
                result_rows = conn.execute(
                    "SELECT id, ts, model, production_pred, shadow_pred, diff_pct, job_id "
                    "FROM shadow_results WHERE lower(model)=? ORDER BY ts DESC, id DESC LIMIT 50",
                    (key,),
                ).fetchall()
            else:
                config_rows = conn.execute(
                    "SELECT model, shadow_alias, enabled, updated_at, updated_by "
                    "FROM shadow_config ORDER BY model",
                ).fetchall()
                result_rows = []
        return {"config": [dict(r) for r in config_rows], "results": [dict(r) for r in result_rows]}
    except Exception:
        return {"config": [], "results": []}


@router.post("/shadow")
async def set_shadow(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Also through the enforcing dependency, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the admin dependency, never in place of it: `require_capability` admits
    # operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(TRAFFIC_MANAGE)),
) -> dict:
    """Enable/disable shadow deployment for a model (admin; audited ``shadow_config_set``).

    Body: ``{model, enabled, shadow_alias?}``. ``enabled=true`` mirrors ``exa serve shadow enable``
    (INSERT OR REPLACE); ``enabled=false`` mirrors ``exa serve shadow disable`` (UPDATE)."""
    _require_manage(principal)
    model = (payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")
    enabled = bool(payload.get("enabled", True))
    shadow_alias = (payload.get("shadow_alias") or "Staging").strip() or "Staging"

    get_db, init_db = _examlops_data()
    from examlops.data.serving import set_shadow_config  # noqa: PLC0415 - guarded above

    init_db()
    actor = principal.get("sub", "?")
    with get_db() as conn:
        _ensure_shadow(conn)
    # The same code path as `exa serve shadow`, so both store the canonical key the Ray replica
    # reads (plan P0.4 / finding B4).
    set_shadow_config(model, shadow_alias=shadow_alias, enabled=enabled, updated_by=actor)
    with get_db() as conn:
        _audit(
            conn,
            actor,
            "shadow_config_set",
            model,
            {"enabled": enabled, "shadow_alias": shadow_alias},
        )
    return {"model": model, "enabled": enabled, "shadow_alias": shadow_alias}
