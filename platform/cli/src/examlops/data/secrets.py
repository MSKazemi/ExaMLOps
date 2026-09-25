"""examlops.data.secrets — Secrets store (D7).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "all_secret_records",
    "get_secret_ciphertext",
    "get_secret_record",
    "list_secret_paths",
    "put_secret_ciphertext",
]


def all_secret_records() -> list[dict[str, Any]]:
    """Every secret's (path, tenant, ciphertext, key_id) — for the rewrap/rotation job (2.3)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT path, tenant, ciphertext, key_id FROM secrets_store ORDER BY tenant, path"
        ).fetchall()
    return [dict(r) for r in rows]


def get_secret_ciphertext(path: str, tenant: str) -> str | None:
    """Return just the ciphertext (back-compat). Prefer :func:`get_secret_record` for the key_id."""
    rec = get_secret_record(path, tenant)
    return rec["ciphertext"] if rec else None


def get_secret_record(path: str, tenant: str) -> dict[str, Any] | None:
    """Return ``{ciphertext, key_id, version}`` for a secret, or None."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT ciphertext, key_id, version FROM secrets_store WHERE path=? AND tenant=?",
            (path, tenant),
        ).fetchone()
    return dict(row) if row else None


def list_secret_paths(tenant: str | None = None) -> list[dict[str, Any]]:
    """List secret metadata (path/tenant/version/updated_at) — never values."""
    init_db()
    with get_db() as conn:
        if tenant:
            rows = conn.execute(
                "SELECT path, tenant, version, updated_by, updated_at FROM secrets_store "
                "WHERE tenant=? ORDER BY path",
                (tenant,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, tenant, version, updated_by, updated_at FROM secrets_store "
                "ORDER BY tenant, path"
            ).fetchall()
    return [dict(r) for r in rows]


def put_secret_ciphertext(
    path: str,
    tenant: str,
    ciphertext: str,
    *,
    updated_by: str | None = None,
    key_id: str | None = None,
    rewrap: bool = False,
) -> int:
    """Upsert an encrypted secret, bumping its version. Returns the new version.

    ``key_id`` records which KEK the ciphertext is wrapped under (2.3), so keys can rotate.

    ``rewrap=True`` re-wraps an *existing* value under another KEK: the ciphertext, key id and
    version change, but ``updated_at``/``updated_by`` keep describing the last time the secret's
    **value** was written. A KEK rotation is not a credential rotation, and ADR 0027's
    ``secrets_rotation`` evidence reads ``updated_at`` as the credential's age — bumping it here
    would let ``exa secrets rewrap`` make a years-old password look freshly rotated.
    """
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT version FROM secrets_store WHERE path=? AND tenant=?", (path, tenant)
        ).fetchone()
        version = (int(row["version"]) + 1) if row else 1
        if rewrap and row is not None:
            conn.execute(
                "UPDATE secrets_store SET ciphertext=?, key_id=?, version=? "
                "WHERE path=? AND tenant=?",
                (ciphertext, key_id, version, path, tenant),
            )
            return version
        conn.execute(
            """INSERT INTO secrets_store
                   (path, tenant, ciphertext, key_id, version, updated_by, updated_at)
               VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(path, tenant) DO UPDATE SET
                   ciphertext=excluded.ciphertext, key_id=excluded.key_id,
                   version=excluded.version, updated_by=excluded.updated_by,
                   updated_at=CURRENT_TIMESTAMP""",
            (path, tenant, ciphertext, key_id, version, updated_by),
        )
    return version


install_write_retry(__name__)
