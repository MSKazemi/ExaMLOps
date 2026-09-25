"""LLM gateway virtual keys + live status (B2, dashboard-rebuild M2; P6 status/test-chat panel).

Reads (viewer): the `virtual_keys` register — only stored fields (key HASH, scope, budget, spend,
revoked); the raw key is never stored and never re-fetchable. Writes (admin + `gateway.manage`,
audited): issue a key (returns the raw key ONCE) and revoke one — reusing the shared
`examlops.gateway.issue_virtual_key` / `examlops.data.governance.revoke_virtual_key` code paths so the
dashboard can't drift from the CLI. Pure platform.db — no live gateway/LLM runtime required.

Live status (viewer) and test-chat (admin) additions proxy the *deployed* llm-gateway service
directly, over HTTP — deliberately through routes that need no admin credential:

- ``GET /gateway/status`` calls the service's own ``GET /ready`` (ADR 0153 d10 latch), which has
  no auth requirement at all. ``GET /admin/health``/``/admin/config`` (per-provider diagnostics,
  the full route table) are NOT proxied here — ``routers/health.py``'s own comment on this same
  service already states the reasoning this follows: those need the gateway's admin bearer, "a
  bearer this process would otherwise have to hold and never expose, for a page that only needs
  up/down" (ADR 0151). `exa gateway providers`/`routes` remain the CLI's job for that depth.
- ``POST /gateway/test-chat`` sends one real chat message through the deployed service's own
  ``POST /v1/chat/completions`` — the same call `exa gateway chat --stream` makes non-streamed —
  using an operator-supplied virtual key (never one the dashboard stores), exactly the CLI's own
  `--key` flag. This keeps the same low blast-radius, revocable, budgeted credential the virtual-
  key system exists for, rather than adding a new secret for the dashboard to custody.
"""

from __future__ import annotations

import json
import sqlite3
import time

import audit_write
import httpx
from auth import require_role
from capabilities import GATEWAY_MANAGE, can, deny_reason, require_capability, scope_to_tenant
from dbconn import connect, platform_db_path
from fastapi import APIRouter, Body, Depends, HTTPException, status

router = APIRouter(prefix="/gateway", tags=["gateway"])
_viewer = require_role("viewer")
_admin = require_role("admin")

_STATUS_TIMEOUT = 8.0  # seconds — matches routers/health.py's own probe budget
_TEST_CHAT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # a cold model load can take a while


def _db_path() -> str:
    return platform_db_path()


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, GATEWAY_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, GATEWAY_MANAGE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    audit_write.audit(actor, action, target, details, conn=conn)


def _examlops_gateway():
    """Lazy, guarded import of the shared gateway code paths (503 if unavailable)."""
    try:
        from examlops import gateway as _g  # type: ignore
        from examlops.data import governance as _gov  # type: ignore

        return _g, _gov
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "gateway writes require the examlops package (not available in this deployment)",
        ) from exc


@router.get("/keys")
async def list_keys(principal: dict = Depends(_viewer)) -> list[dict]:
    """Virtual keys — stored fields only (hash, scope, budget, spend, revoked). Never the raw key.

    Scoped to the caller's tenant (F15 R4). The raw key was never returned, but the project a
    key belongs to and what it has spent were visible to every other tenant.
    """
    try:
        conn = connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT key_hash, tenant, project, models_json, budget_usd, spent_usd, created_by, "
                "created_at, revoked FROM virtual_keys ORDER BY created_at DESC"
            ).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["models"] = json.loads(d.pop("models_json") or "[]")
                d["revoked"] = bool(d["revoked"])
                out.append(d)
            return scope_to_tenant(principal, out)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # A missing table just means nothing was recorded yet (D12); any other
        # datastore failure must surface, not masquerade as an empty list.
        if "no such table" in str(exc).lower():
            return []
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "datastore unavailable") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "datastore unavailable") from exc


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def issue_key(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(GATEWAY_MANAGE)),
) -> dict:
    """Issue a virtual key (admin; audited). Returns the raw key **once** — it is never stored.

    Body: ``{tenant?, project?, models?: string[], budgetUsd?: number}``. Mirrors
    ``exa gateway key issue`` via the shared ``examlops.gateway.issue_virtual_key`` (audits
    ``source=dashboard``). The raw key is returned here and nowhere else — only its SHA-256 hash is
    persisted, so the UI must surface it once and never re-fetch it.
    """
    _require_manage(principal)
    tenant = (payload.get("tenant") or "default").strip() or "default"
    project = (payload.get("project") or "default").strip() or "default"
    models = payload.get("models") or None
    if models is not None and not isinstance(models, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "models must be a list of model names")
    budget = payload.get("budgetUsd")
    try:
        budget_usd = float(budget) if budget is not None and budget != "" else None
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "budgetUsd must be a number") from None
    gateway, _gov = _examlops_gateway()
    raw = gateway.issue_virtual_key(
        tenant, project, models, budget_usd, principal.get("sub", "?"), source="dashboard"
    )
    return {
        "key": raw,  # shown ONCE — never stored, never re-fetchable
        "tenant": tenant,
        "project": project,
        "models": models or [],
        "budgetUsd": budget_usd,
    }


@router.post("/keys/{key_hash}/revoke")
async def revoke_key(
    key_hash: str,
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(GATEWAY_MANAGE)),
) -> dict:
    """Revoke a virtual key by its hash (admin; audited). Mirrors ``exa gateway key revoke``."""
    _require_manage(principal)
    _gateway, gov = _examlops_gateway()
    gov.revoke_virtual_key(key_hash)
    conn = connect(_db_path())
    try:
        _audit(conn, principal.get("sub", "?"), "virtual_key_revoked", key_hash, {})
        conn.commit()
        conn.close()
        return {"keyHash": key_hash, "revoked": True}
    finally:
        conn.close()


@router.get("/status")
async def gateway_status(principal: dict = Depends(_viewer)) -> dict:
    """Live per-route health of the *deployed* llm-gateway (proxies its own `GET /ready`).

    No admin credential involved — `/ready` needs none (see module docstring). Never raises on an
    unreachable gateway: a down or misconfigured service is a normal, displayable state, not a
    dashboard error.
    """
    from settings import settings

    url = f"{settings.llm_gateway_url.rstrip('/')}/ready"
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(url, timeout=_STATUS_TIMEOUT)
        body = r.json()
        return {
            "reachable": True,
            "ready": bool(body.get("ready")),
            "healthyNow": bool(body.get("healthyNow", body.get("healthy_now"))),
            "routes": body.get("routes", {}),
            "warnings": body.get("warnings", []),
        }
    except httpx.HTTPError:
        return {
            "reachable": False,
            "ready": False,
            "healthyNow": False,
            "routes": {},
            "warnings": [],
        }
    except (ValueError, TypeError):
        # A response came back but wasn't the JSON shape expected — surfaced as unreachable
        # rather than a 500; the CLI's own `exa gateway status` is the tool for diagnosing why.
        return {
            "reachable": False,
            "ready": False,
            "healthyNow": False,
            "routes": {},
            "warnings": [],
        }


@router.post("/test-chat")
async def test_chat(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    _gate: dict = Depends(require_capability(GATEWAY_MANAGE)),
) -> dict:
    """Send one real chat message through the deployed gateway (admin; audited).

    Body: ``{message: str, route?: str, key?: str}``. Mirrors `exa gateway chat --key` against the
    live service's own `POST /v1/chat/completions` (never the in-process `GatewayClient` shortcut
    the plain CLI `exa gateway chat` uses) — this is meant to prove the *deployed* gateway answers,
    not the library. `key` is an operator-supplied virtual key, exactly the CLI's `--key` flag; the
    dashboard never stores or reuses it, and it is never written to the audit row.
    """
    from settings import settings

    _require_manage(principal)
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message must not be empty")
    route = str(payload.get("route") or "default").strip() or "default"
    key = payload.get("key")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = f"{settings.llm_gateway_url.rstrip('/')}/v1/chat/completions"
    body = {"model": route, "messages": [{"role": "user", "content": message}], "stream": False}
    started = time.monotonic()
    result: dict
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=body, headers=headers, timeout=_TEST_CHAT_TIMEOUT)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        data = r.json()
        if r.status_code < 400:
            reply = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            result = {"ok": True, "status": r.status_code, "reply": reply, "latencyMs": latency_ms}
        else:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            result = {
                "ok": False,
                "status": r.status_code,
                "error": err.get("message") or r.text[:500],
                "code": err.get("code"),
                "latencyMs": latency_ms,
            }
    except httpx.HTTPError as exc:
        result = {
            "ok": False,
            "status": None,
            "error": f"gateway unreachable: {exc}",
            "code": "gateway_unreachable",
            "latencyMs": round((time.monotonic() - started) * 1000, 1),
        }
    conn = connect(_db_path())
    try:
        # Route/outcome/latency only — never the message text or the reply, and never the key.
        _audit(
            conn,
            principal.get("sub", "?"),
            "gateway_test_chat",
            route,
            {"ok": result["ok"], "status": result["status"], "latencyMs": result["latencyMs"]},
        )
        conn.commit()
    finally:
        conn.close()
    return result
