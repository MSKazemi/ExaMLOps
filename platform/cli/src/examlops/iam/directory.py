"""The federated account directory: who a center has provisioned, and who it has taken away (ADR 0132).

A center's IdP decides who may sign in, but a valid access token outlives the decision to remove
someone: until it expires (minutes to an hour) a leaver keeps access. NIST SP 800-63C-4 therefore
has the IdP *de-provision* relying-party accounts through a provisioning API. This module is the
relying party's side of that: one row per federated account, keyed by ``(provider, subject)`` once
the account has signed in, and by the provider's own identifier (``userName`` / ``externalId``)
before it has.

Two provisioning modes per provider (``provisioning.mode`` in the trust file):

* ``jit`` (default) — accounts appear on first sign-in; the center (over SCIM) or an operator
  (``exa auth deactivate``) can still deactivate one, and a deactivated or deleted account is
  refused even with a valid token.
* ``scim`` — strict: only accounts the center pushed over SCIM may sign in. An unknown account is
  refused, the way an enterprise app assigned through Entra ID or Okta behaves.

Enforcement is read-mostly and cached (``EXAMLOPS_IAM_ACCOUNT_CACHE_TTL``, default 10 s), so a
deprovisioning takes effect across every process within that window and immediately in the process
that received it. A deleted account leaves a tombstone: deleting the row would let the next
just-in-time sign-in resurrect it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from examlops.data import get_db

logger = logging.getLogger(__name__)

_TS = "%Y-%m-%dT%H:%M:%SZ"


class AccountDenied(RuntimeError):
    """The account is deactivated, deleted, or (strict mode) not provisioned."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConflictError(ValueError):
    """A provisioning request collides with an existing account (SCIM ``uniqueness``)."""


@dataclass
class Account:
    id: str
    provider: str
    subject: str | None = None
    username: str | None = None
    email: str | None = None
    external_id: str | None = None
    display_name: str | None = None
    active: bool = True
    source: str = "jit"  # jit | scim | cli
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str | None = None
    deactivated_at: str | None = None
    deactivated_by: str | None = None
    deleted_at: str | None = None
    version: int = 1

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "subject": self.subject,
            "username": self.username,
            "email": self.email,
            "external_id": self.external_id,
            "display_name": self.display_name,
            "active": self.active,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seen_at": self.last_seen_at,
            "deactivated_at": self.deactivated_at,
            "deactivated_by": self.deactivated_by,
        }


def _now() -> str:
    return datetime.now(UTC).strftime(_TS)


_init_lock = threading.Lock()
_initialized: set[str] = set()


def _ensure_table() -> None:
    key = os.getenv("PLATFORM_DB", "") + "|" + os.getenv("EXAMLOPS_DB_BACKEND", "")
    if key in _initialized:
        return
    with _init_lock:
        if key in _initialized:
            return
        with get_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS iam_accounts (
                       id              TEXT PRIMARY KEY,
                       provider        TEXT NOT NULL,
                       subject         TEXT,              -- OIDC sub, once the account signed in
                       username        TEXT,
                       email           TEXT,
                       external_id     TEXT,              -- the IdP's own id (SCIM externalId)
                       display_name    TEXT,
                       active          INTEGER NOT NULL DEFAULT 1,
                       source          TEXT NOT NULL DEFAULT 'jit',
                       attributes_json TEXT NOT NULL DEFAULT '{}',
                       created_at      TEXT NOT NULL,
                       updated_at      TEXT NOT NULL,
                       last_seen_at    TEXT,
                       deactivated_at  TEXT,
                       deactivated_by  TEXT,
                       deleted_at      TEXT,              -- tombstone: never resurrected by JIT
                       version         INTEGER NOT NULL DEFAULT 1
                   )"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_iam_accounts_subject "
                "ON iam_accounts(provider, subject)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_iam_accounts_username "
                "ON iam_accounts(provider, username)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_iam_accounts_external "
                "ON iam_accounts(provider, external_id)"
            )
        _initialized.add(key)


def _row(r: Any) -> Account:
    return Account(
        id=r["id"],
        provider=r["provider"],
        subject=r["subject"],
        username=r["username"],
        email=r["email"],
        external_id=r["external_id"],
        display_name=r["display_name"],
        active=bool(r["active"]),
        source=r["source"],
        attributes=json.loads(r["attributes_json"] or "{}"),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        last_seen_at=r["last_seen_at"],
        deactivated_at=r["deactivated_at"],
        deactivated_by=r["deactivated_by"],
        deleted_at=r["deleted_at"],
        version=int(r["version"]),
    )


def _audit(
    action: str, account: Account, actor: str, details: dict[str, Any] | None = None
) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "iam",
            actor,
            action,
            f"{account.provider}:{account.subject or account.username or account.id}",
            {"account_id": account.id, "provider": account.provider, **(details or {})},
        )
    except Exception as exc:  # noqa: BLE001 — never block provisioning on audit, never lose it silently
        logger.warning("iam account audit write failed (%s %s): %s", action, account.id, exc)


# ── lookups ──────────────────────────────────────────────────────────────────


def get(account_id: str, *, include_deleted: bool = False) -> Account | None:
    _ensure_table()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM iam_accounts WHERE id=?", (account_id,)).fetchone()
    if r is None:
        return None
    acc = _row(r)
    return acc if include_deleted or acc.deleted_at is None else None


def find(
    provider: str,
    *,
    subject: str | None = None,
    username: str | None = None,
    email: str | None = None,
    external_id: str | None = None,
    include_deleted: bool = True,
) -> Account | None:
    """The account a principal or a provisioning request refers to (first identifier that matches)."""
    _ensure_table()
    probes = [
        ("subject", subject),
        ("external_id", external_id),
        ("username", username),
        ("email", email),
    ]
    with get_db() as conn:
        for column, value in probes:
            if not value:
                continue
            r = conn.execute(
                f"SELECT * FROM iam_accounts WHERE provider=? AND {column}=? "  # noqa: S608 — fixed columns
                "ORDER BY deleted_at IS NOT NULL, updated_at DESC LIMIT 1",
                (provider, value),
            ).fetchone()
            if r is not None:
                acc = _row(r)
                if include_deleted or acc.deleted_at is None:
                    return acc
    return None


def list_accounts(
    provider: str | None = None,
    *,
    active: bool | None = None,
    include_deleted: bool = False,
    filters: dict[str, str] | None = None,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[Account], int]:
    """Accounts (newest first) plus the total count, for listing and SCIM ``ListResponse``."""
    _ensure_table()
    where: list[str] = []
    args: list[Any] = []
    if provider:
        where.append("provider=?")
        args.append(provider)
    if active is not None:
        where.append("active=?")
        args.append(1 if active else 0)
    if not include_deleted:
        where.append("deleted_at IS NULL")
    for column, value in (filters or {}).items():
        if column not in {"id", "username", "email", "external_id", "subject"}:
            raise ValueError(f"cannot filter on {column!r}")
        where.append(f"{column}=?")
        args.append(value)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM iam_accounts{clause}", args).fetchone()[
            "n"
        ]  # noqa: S608
        rows = conn.execute(
            f"SELECT * FROM iam_accounts{clause} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",  # noqa: S608
            [*args, max(0, limit), max(0, offset)],
        ).fetchall()
    return [_row(r) for r in rows], int(total)


# ── mutations ────────────────────────────────────────────────────────────────


def _write(acc: Account, *, insert: bool) -> Account:
    acc.updated_at = _now()
    values = (
        acc.provider,
        acc.subject,
        acc.username,
        acc.email,
        acc.external_id,
        acc.display_name,
        1 if acc.active else 0,
        acc.source,
        json.dumps(acc.attributes, sort_keys=True),
        acc.updated_at,
        acc.last_seen_at,
        acc.deactivated_at,
        acc.deactivated_by,
        acc.deleted_at,
    )
    with get_db() as conn:
        if insert:
            acc.created_at = acc.created_at or acc.updated_at
            conn.execute(
                """INSERT INTO iam_accounts (provider, subject, username, email, external_id,
                       display_name, active, source, attributes_json, updated_at, last_seen_at,
                       deactivated_at, deactivated_by, deleted_at, id, created_at, version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, acc.id, acc.created_at, acc.version),
            )
        else:
            acc.version += 1
            conn.execute(
                """UPDATE iam_accounts SET provider=?, subject=?, username=?, email=?,
                       external_id=?, display_name=?, active=?, source=?, attributes_json=?,
                       updated_at=?, last_seen_at=?, deactivated_at=?, deactivated_by=?,
                       deleted_at=?, version=? WHERE id=?""",
                (*values, acc.version, acc.id),
            )
    invalidate(acc.provider)
    return acc


def provision(
    provider: str,
    *,
    username: str | None,
    email: str | None = None,
    external_id: str | None = None,
    display_name: str | None = None,
    active: bool = True,
    attributes: dict[str, Any] | None = None,
    actor: str = "scim",
    source: str = "scim",
) -> Account:
    """Create an account (SCIM ``POST /Users``). A tombstoned account with the same key is revived."""
    existing = find(provider, username=username, external_id=external_id)
    if existing is not None and existing.deleted_at is None:
        raise ConflictError(f"an account {username or external_id!r} already exists for {provider}")
    if existing is not None:  # revive the tombstone rather than create a twin
        existing.deleted_at = None
        acc = existing
        acc.username, acc.email, acc.external_id = username, email, external_id
        acc.display_name, acc.attributes = display_name, dict(attributes or {})
        acc.active, acc.source = active, source
        acc.deactivated_at = None if active else _now()
        acc.deactivated_by = None if active else actor
        _write(acc, insert=False)
    else:
        acc = Account(
            id=uuid.uuid4().hex,
            provider=provider,
            username=username,
            email=email,
            external_id=external_id,
            display_name=display_name,
            active=active,
            source=source,
            attributes=dict(attributes or {}),
            deactivated_at=None if active else _now(),
            deactivated_by=None if active else actor,
        )
        _write(acc, insert=True)
    _audit("iam_account_provisioned", acc, actor, {"active": acc.active, "source": source})
    return acc


def update(account: Account, *, actor: str = "scim", **changes: Any) -> Account:
    """Apply field changes (SCIM ``PUT``/``PATCH``); an ``active`` flip is audited on its own."""
    was_active = account.active
    for key, value in changes.items():
        if key not in {"username", "email", "external_id", "display_name", "active", "attributes"}:
            raise ValueError(f"unknown account field {key!r}")
        setattr(account, key, value)
    if was_active and not account.active:
        account.deactivated_at, account.deactivated_by = _now(), actor
    elif not was_active and account.active:
        account.deactivated_at, account.deactivated_by = None, None
    _write(account, insert=False)
    if was_active != account.active:
        _audit(
            "iam_account_activated" if account.active else "iam_account_deactivated",
            account,
            actor,
        )
    else:
        _audit("iam_account_updated", account, actor, {"fields": sorted(changes)})
    return account


def deactivate(account: Account, *, actor: str, reason: str = "") -> Account:
    if not account.active:
        return account
    account.active = False
    account.deactivated_at, account.deactivated_by = _now(), actor
    _write(account, insert=False)
    _audit("iam_account_deactivated", account, actor, {"reason": reason})
    return account


def activate(account: Account, *, actor: str) -> Account:
    if account.active and account.deleted_at is None:
        return account
    account.active, account.deleted_at = True, None
    account.deactivated_at = account.deactivated_by = None
    _write(account, insert=False)
    _audit("iam_account_activated", account, actor)
    return account


def delete(account: Account, *, actor: str) -> None:
    """SCIM ``DELETE``: gone from listings, and refused forever after — a tombstone, not a delete."""
    account.active = False
    account.deleted_at = _now()
    account.deactivated_at = account.deactivated_at or account.deleted_at
    account.deactivated_by = account.deactivated_by or actor
    _write(account, insert=False)
    _audit("iam_account_deleted", account, actor)


# ── enforcement ──────────────────────────────────────────────────────────────


def _cache_ttl() -> float:
    try:
        return float(os.getenv("EXAMLOPS_IAM_ACCOUNT_CACHE_TTL", "") or 10.0)
    except ValueError:
        return 10.0


_cache_lock = threading.Lock()
_status_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_last_seen_written: dict[tuple[str, str], float] = {}
_LAST_SEEN_EVERY = 300.0


def invalidate(provider: str | None = None) -> None:
    with _cache_lock:
        if provider is None:
            _status_cache.clear()
        else:
            for key in [k for k in _status_cache if k[0] == provider]:
                del _status_cache[key]


def _identifiers(principal: Any) -> dict[str, str | None]:
    claims = getattr(principal, "claims", {}) or {}
    return {
        "subject": principal.subject,
        "username": principal.username or None,
        "email": principal.email or None,
        "external_id": str(claims.get("oid") or claims.get("external_id") or "") or None,
    }


def check(principal: Any, mode: str = "jit", *, record: bool = True) -> None:
    """Refuse a principal whose account is deactivated, deleted, or (``scim`` mode) unprovisioned.

    With ``record`` (every enforcement point), also records the account on first sight (JIT), links
    a SCIM-provisioned account to its OIDC ``sub`` the first time that user signs in, and refreshes
    ``last_seen_at`` at most every 5 min. ``record=False`` is for inspection tools (``exa auth
    verify``): same verdict, no writes. Datastore errors fail **closed** in ``scim`` mode and open
    (logged) in ``jit`` mode, where the IdP stays the authority and token lifetime bounds exposure.
    """
    key = (principal.provider, principal.subject)
    now = time.monotonic()
    with _cache_lock:
        hit = _status_cache.get(key)
    if hit is not None and hit[0] > now:
        if hit[1] is not None:
            raise AccountDenied(hit[1])
        return
    try:
        denial = _evaluate(principal, mode, record)
    except Exception as exc:  # noqa: BLE001
        if mode == "scim":
            raise AccountDenied(
                f"account directory unavailable ({exc.__class__.__name__})"
            ) from exc
        logger.warning("account directory unavailable, allowing %s (jit mode): %s", key, exc)
        return
    if record:
        with _cache_lock:
            if len(_status_cache) > 50_000:
                _status_cache.clear()
            _status_cache[key] = (now + _cache_ttl(), denial)
    if denial is not None:
        raise AccountDenied(denial)


def _evaluate(principal: Any, mode: str, record: bool) -> str | None:
    ids = _identifiers(principal)
    acc = find(
        principal.provider,
        subject=ids["subject"],
        username=ids["username"],
        email=ids["email"],
        external_id=ids["external_id"],
    )
    if acc is not None and acc.subject not in (None, principal.subject):
        # Matched by username/email but bound to ANOTHER sub: a different person (a reused
        # username at the center). Neither inherit that account's state nor take it over.
        acc = find(principal.provider, subject=principal.subject)
    if acc is None:
        if mode == "scim":
            return "your account has not been provisioned for ExaMLOps by your organisation"
        if record:
            _write(
                Account(
                    id=uuid.uuid4().hex,
                    provider=principal.provider,
                    subject=principal.subject,
                    username=ids["username"],
                    email=ids["email"],
                    external_id=ids["external_id"],
                    display_name=principal.username or None,
                    source="jit",
                    last_seen_at=_now(),
                ),
                insert=True,
            )
        return None
    if acc.deleted_at is not None:
        return "your account was removed from ExaMLOps by your organisation"
    if not acc.active:
        return "your account has been deactivated"
    if not record:
        return None
    key = (principal.provider, principal.subject)
    stale = time.monotonic() - _last_seen_written.get(key, -1e9) > _LAST_SEEN_EVERY
    if acc.subject is None or stale:
        acc.subject = principal.subject  # links a SCIM-provisioned account on its first sign-in
        acc.last_seen_at = _now()
        _write(acc, insert=False)
        _last_seen_written[key] = time.monotonic()
    return None
