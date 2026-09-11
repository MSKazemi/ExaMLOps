"""SCIM 2.0 provisioning endpoint for the data centers' IdPs — ADR 0132.

Mounted at ``/api/scim/v2``; configure that URL (e.g. ``https://<dashboard>/api/scim/v2``) as the
"tenant URL" in the center's IdP, with the bearer named by ``provisioning.token_ref`` in its trust
entry. The protocol logic lives in ``examlops.iam.scim`` (shared with any other service); this router
only adapts HTTP.

Not a dashboard-session API: every route authenticates the **center's SCIM client** by its own
bearer and is confined to that center's accounts. Deactivations take effect on every platform
service within the account-cache TTL (default 10 s) and immediately end the user's dashboard
sessions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Header, Query, Response
from fastapi.responses import JSONResponse
from settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scim/v2", tags=["scim"])


def _scim():
    from examlops.iam import scim  # type: ignore

    return scim


def _base_url() -> str:
    return settings.public_dashboard_url.rstrip("/") + "/api/scim/v2"


def _json(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, media_type="application/scim+json")


def _run(authorization: str | None, op: Callable[[Any], Any], status: int = 200) -> Response:
    """Authenticate the center's SCIM client, run ``op(provider)``, map errors to RFC 7644 §3.12."""
    scim = _scim()
    try:
        provider = scim.authenticate(authorization)
        result = op(provider)
    except scim.ScimError as exc:
        headers = {"WWW-Authenticate": 'Bearer realm="scim"'} if exc.status == 401 else None
        resp = _json(exc.status, exc.body())
        if headers:
            resp.headers.update(headers)
        return resp
    except Exception as exc:  # noqa: BLE001 — a SCIM client needs a SCIM error, never an HTML 500
        logger.exception("SCIM request failed")
        return _json(500, scim.ScimError(500, f"internal error: {exc.__class__.__name__}").body())
    if result is None:
        return Response(status_code=204)
    return _json(status, result)


@router.get("/ServiceProviderConfig", summary="SCIM service provider configuration")
def service_provider_config(authorization: str | None = Header(default=None)) -> Response:
    return _run(authorization, lambda _p: _scim().service_provider_config(_base_url()))


@router.get("/ResourceTypes", summary="SCIM resource types")
def resource_types(authorization: str | None = Header(default=None)) -> Response:
    return _run(authorization, lambda _p: _scim().resource_types(_base_url()))


@router.get("/Schemas", summary="SCIM schemas")
def schemas(authorization: str | None = Header(default=None)) -> Response:
    return _run(authorization, lambda _p: _scim().schemas(_base_url()))


@router.get("/Users", summary="List/filter the center's provisioned accounts")
def list_users(
    authorization: str | None = Header(default=None),
    filter: str | None = Query(default=None),  # noqa: A002 — SCIM's parameter name
    startIndex: int = Query(default=1),  # noqa: N803 — SCIM's parameter name
    count: int = Query(default=100),
) -> Response:
    return _run(
        authorization,
        lambda p: _scim().list_users(
            p, _base_url(), filter=filter, start_index=startIndex, count=count
        ),
    )


@router.post("/Users", summary="Provision an account")
def create_user(
    payload: dict[str, Any] = Body(...), authorization: str | None = Header(default=None)
) -> Response:
    return _run(authorization, lambda p: _scim().create_user(p, _base_url(), payload), status=201)


@router.get("/Users/{account_id}", summary="Read one provisioned account")
def get_user(account_id: str, authorization: str | None = Header(default=None)) -> Response:
    return _run(authorization, lambda p: _scim().get_user(p, _base_url(), account_id))


@router.put("/Users/{account_id}", summary="Replace an account")
def replace_user(
    account_id: str,
    payload: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
) -> Response:
    return _run(authorization, lambda p: _scim().replace_user(p, _base_url(), account_id, payload))


@router.patch("/Users/{account_id}", summary="Update an account (e.g. active: false)")
def patch_user(
    account_id: str,
    payload: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
) -> Response:
    return _run(authorization, lambda p: _scim().patch_user(p, _base_url(), account_id, payload))


@router.delete(
    "/Users/{account_id}", summary="Deprovision an account (tombstoned, refused forever)"
)
def delete_user(account_id: str, authorization: str | None = Header(default=None)) -> Response:
    return _run(authorization, lambda p: _scim().delete_user(p, account_id))
