"""Dynamic, short-lived credentials from OpenBao/Vault — ADR 0011 clause 3 ("where supported").

A *dynamic* secrets engine (``database/creds/<role>``, ``aws/creds/<role>``, ``rabbitmq/creds/…``)
mints a fresh credential per read, bound to a **lease** the manager revokes when its TTL runs out.
Nothing here configures an engine — that is the operator's OpenBao policy — but once one is
mounted the platform can:

* :func:`issue` — read the engine path, returning the credential fields and the lease
  (``lease_id``, ``lease_duration``, ``renewable``);
* :func:`renew` — extend a lease (``sys/leases/renew``), bounded by the engine's max TTL;
* :func:`revoke` — end it early (``sys/leases/revoke``), e.g. when a job finishes.

Every call is audited (``secret_lease_issued`` / ``_renewed`` / ``_revoked``) with the lease id and
TTL, never the credential. Every failure raises :class:`LeaseError` (fail closed): a caller that
asked for a short-lived credential must not silently get a long-lived one from another tier.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

MAX_TTL_SECONDS = 7 * 24 * 3600  # a requested TTL/increment above a week is refused client-side


class LeaseError(RuntimeError):
    """A dynamic-credential operation could not be completed."""


def _addr() -> str:
    addr = os.getenv("EXAMLOPS_VAULT_ADDR", "").strip().rstrip("/")
    if not addr:
        raise LeaseError("dynamic credentials need OpenBao/Vault: EXAMLOPS_VAULT_ADDR is not set")
    return addr


def _request(method: str, api_path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from examlops.secrets import _vault_headers, _vault_timeout

    data = json.dumps(body).encode() if body is not None else None
    headers = {**_vault_headers(), "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{_addr()}/v1/{api_path.lstrip('/')}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=_vault_timeout()) as resp:  # noqa: S310
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise LeaseError(f"vault refused {method} {api_path}: HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - any transport failure fails the operation
        raise LeaseError(
            f"vault unreachable for {method} {api_path}: {type(exc).__name__}"
        ) from exc
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise LeaseError(f"vault returned a non-JSON reply to {method} {api_path}") from exc


def _check_ttl(seconds: int | None) -> None:
    if seconds is not None and not (0 < seconds <= MAX_TTL_SECONDS):
        raise LeaseError(f"TTL must be between 1 and {MAX_TTL_SECONDS} seconds, got {seconds}")


def _audit(action: str, target: str, actor: str | None, extra: dict[str, Any]) -> None:
    from examlops.data.audit import audit_best_effort

    audit_best_effort("exa-secrets", actor, action, target, extra)


def issue(engine_path: str, *, actor: str | None = None, tenant: str = "default") -> dict[str, Any]:
    """Mint a dynamic credential at ``engine_path`` (e.g. ``database/creds/readonly``).

    Returns ``{lease_id, lease_duration, renewable, data}``; ``data`` holds the credential and is
    the caller's to protect. Tenant scoping is the same path-prefix rule as a static read.
    """
    from examlops.secrets import InvalidSecretPath, SecretAccessDenied, _check_path, _tenant_allowed

    path = engine_path.strip().strip("/")
    try:
        _check_path(path)
    except InvalidSecretPath as exc:
        raise LeaseError(f"invalid engine path: {engine_path!r}") from exc
    if not _tenant_allowed(path, tenant):
        _audit("secret_denied", path, actor, {"tenant": tenant, "op": "lease"})
        raise SecretAccessDenied(f"tenant '{tenant}' may not issue credentials at '{path}'")
    from urllib.parse import quote

    reply = _request("GET", quote(path, safe="/"))
    lease_id = reply.get("lease_id") or ""
    if not lease_id:
        # A static KV read has no lease. Handing it back as "short-lived" would be a false claim.
        raise LeaseError(f"'{path}' returned no lease - it is not a dynamic secrets engine")
    fields: dict[str, Any] = dict(reply.get("data") or {})
    out: dict[str, Any] = {
        "lease_id": lease_id,
        "lease_duration": int(reply.get("lease_duration") or 0),
        "renewable": bool(reply.get("renewable")),
        "data": fields,
    }
    _audit(
        "secret_lease_issued",
        path,
        actor,
        {
            "tenant": tenant,
            "lease_id": lease_id,
            "ttl": out["lease_duration"],
            "renewable": out["renewable"],
            "fields": sorted(fields),
        },
    )
    return out


def _check_lease_tenant(lease_id: str, tenant: str, actor: str | None, op: str) -> None:
    """A lease id is ``<engine path>/<uuid>``, so the issue-time tenant rule applies to it too.

    Without this, tenant ``acme`` could renew — or revoke, cutting off a running job — a lease
    that tenant ``globex`` was issued.
    """
    from examlops.secrets import InvalidSecretPath, SecretAccessDenied, _check_path, _tenant_allowed

    if not lease_id.strip():
        raise LeaseError("lease id is empty")
    try:
        _check_path(lease_id)
    except InvalidSecretPath as exc:
        raise LeaseError(f"invalid lease id: {lease_id!r}") from exc
    if not _tenant_allowed(lease_id, tenant):
        _audit("secret_denied", lease_id, actor, {"tenant": tenant, "op": op})
        raise SecretAccessDenied(f"tenant '{tenant}' may not {op} lease '{lease_id}'")


def renew(
    lease_id: str,
    *,
    increment: int | None = None,
    actor: str | None = None,
    tenant: str = "default",
) -> dict:
    """Extend a lease; returns ``{lease_id, lease_duration, renewable}``."""
    _check_lease_tenant(lease_id, tenant, actor, "renew")
    _check_ttl(increment)
    body: dict[str, Any] = {"lease_id": lease_id}
    if increment is not None:
        body["increment"] = increment
    reply = _request("PUT", "sys/leases/renew", body)
    out = {
        "lease_id": reply.get("lease_id") or lease_id,
        "lease_duration": int(reply.get("lease_duration") or 0),
        "renewable": bool(reply.get("renewable")),
    }
    _audit(
        "secret_lease_renewed", lease_id, actor, {"tenant": tenant, "ttl": out["lease_duration"]}
    )
    return out


def revoke(lease_id: str, *, actor: str | None = None, tenant: str = "default") -> None:
    """Revoke a lease now — the credential stops working at the manager."""
    _check_lease_tenant(lease_id, tenant, actor, "revoke")
    _request("PUT", "sys/leases/revoke", {"lease_id": lease_id})
    _audit("secret_lease_revoked", lease_id, actor, {"tenant": tenant})
