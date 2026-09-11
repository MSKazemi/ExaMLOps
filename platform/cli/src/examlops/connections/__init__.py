"""P2 — Named Connections (ADR 0087).

First-class, reusable, project-scoped **data connections** (the RHOAI *connections* analogue): a
project defines an S3 / URI / dataplane source once, and many workloads consume it by name. Non-secret
config is stored inline in ``platform.db``; credentials live only in the D7 secrets client
(:mod:`examlops.secrets`) and are referenced by ``secret_ref`` — never copied into the DB.

Self-contained (own idempotent table) so it composes with the unified Project workspace (ADR 0086)
without touching the shared schema init. A connection is also registered as a project resource
(``kind='connection'``).
"""

from __future__ import annotations

import json
from typing import Any

from examlops.data import get_db

BASE_KINDS = ("s3", "uri", "dataplane")
KINDS = BASE_KINDS  # back-compat alias; prefer kinds()


def kinds() -> tuple[str, ...]:
    """Every connection kind something can consume: the base kinds plus each dataplane
    connector's ``connection_kinds`` (phase-42 registry integration, task 14).
    """
    try:
        from examlops.dataplane.connectors.registry import connection_kinds

        extra = connection_kinds()
    except Exception:  # the dataplane must never break connection management
        extra = ()
    return tuple(sorted(set(BASE_KINDS) | set(extra)))


def _connectors_accepting(kind: str) -> list[Any]:
    try:
        from examlops.dataplane.connectors.registry import all_connectors
    except Exception:
        return []
    return [c for c in all_connectors() if kind in c.connection_kinds and c.available()[0]]


class ConnectionError(Exception):
    """Raised for unknown/invalid connections."""


def _ensure_table() -> None:
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS connections (
                   name        TEXT NOT NULL,
                   project     TEXT NOT NULL DEFAULT '',   -- '' = facility-global
                   kind        TEXT NOT NULL,              -- s3 | uri | dataplane
                   config_json TEXT NOT NULL DEFAULT '{}', -- non-secret config only
                   secret_ref  TEXT,                       -- pointer into examlops.secrets (never the value)
                   created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   created_by  TEXT,
                   PRIMARY KEY (project, name)
               )"""
        )


def create_connection(
    name: str,
    kind: str,
    *,
    project: str | None = None,
    config: dict[str, Any] | None = None,
    secret_value: str | None = None,
    created_by: str | None = None,
    tenant: str = "default",
) -> dict[str, Any]:
    """Create a named connection. Stores non-secret ``config`` inline; if ``secret_value`` is given
    it is written to the D7 secrets client and only its ``secret_ref`` is kept here.
    """
    if kind not in kinds():
        raise ConnectionError(f"unknown kind {kind!r} (expected one of {list(kinds())})")
    _ensure_table()
    proj = project or ""
    secret_ref: str | None = None
    if secret_value is not None:
        from examlops import secrets as _secrets

        secret_ref = f"connections/{proj or 'global'}/{name}"
        _secrets.set_secret(secret_ref, secret_value, tenant=tenant, actor=created_by)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO connections (name, project, kind, config_json, secret_ref, created_by)
               VALUES (?,?,?,?,?,?)""",
            (name, proj, kind, json.dumps(config or {}), secret_ref, created_by),
        )
    # Register as a project resource (ADR 0086) when scoped to a project.
    if project:
        from examlops.data.projects import assign_resource_to_project

        assign_resource_to_project(project, "connection", name, added_by=created_by)
    return get_connection(name, project=project)  # type: ignore[return-value]


def get_connection(name: str, *, project: str | None = None) -> dict[str, Any] | None:
    """Return a connection row (with parsed ``config``), or None. ``secret_ref`` is exposed but the
    secret value is never returned here — use :func:`resolve_connection`.
    """
    _ensure_table()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE project=? AND name=?", (project or "", name)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d.pop("config_json") or "{}")
    d["has_secret"] = bool(d.get("secret_ref"))
    return d


def list_connections(project: str | None = None) -> list[dict[str, Any]]:
    """List connections (metadata only, never secret values). ``project=None`` lists all."""
    _ensure_table()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM connections ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM connections WHERE project=? ORDER BY name", (project or "",)
            ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json") or "{}")
        d["has_secret"] = bool(d.get("secret_ref"))
        out.append(d)
    return out


def delete_connection(name: str, *, project: str | None = None) -> bool:
    """Delete a connection (does NOT delete the referenced secret). Returns True if removed."""
    _ensure_table()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM connections WHERE project=? AND name=?", (project or "", name)
        )
        removed = cur.rowcount > 0
    if removed and project:
        from examlops.data.projects import remove_project_resource

        remove_project_resource(project, "connection", name)
    return removed


def resolve_connection(
    name: str, *, project: str | None = None, tenant: str = "default", actor: str | None = None
) -> dict[str, Any]:
    """Resolve a connection to a usable config dict, injecting the secret from the D7 client.

    The returned dict merges the stored non-secret ``config`` with a ``secret`` key (only when a
    ``secret_ref`` is set). Raises :class:`ConnectionError` if the connection is unknown.
    """
    c = get_connection(name, project=project)
    if not c:
        raise ConnectionError(f"connection {name!r} not found (project={project or '-'})")
    resolved = dict(c["config"])
    resolved["kind"] = c["kind"]
    if c.get("secret_ref"):
        from examlops import secrets as _secrets

        resolved["secret"] = _secrets.get_secret(c["secret_ref"], tenant=tenant, actor=actor)
    return resolved


def test_connection(name: str, *, project: str | None = None) -> dict[str, Any]:
    """Read-only reachability probe. Never prints/returns secret values.

    Returns ``{"ok": bool, "detail": str}``. Degrades gracefully when optional deps are missing.
    """
    c = get_connection(name, project=project)
    if not c:
        return {"ok": False, "detail": f"connection {name!r} not found"}
    kind, cfg = c["kind"], c["config"]
    try:
        if kind == "uri":
            import urllib.request

            url = cfg.get("uri") or cfg.get("url")
            if not url:
                return {"ok": False, "detail": "no 'uri' in config"}
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return {"ok": 200 <= resp.status < 400, "detail": f"HTTP {resp.status}"}
        if kind == "dataplane":
            import urllib.request

            base = cfg.get("endpoint") or cfg.get("url")
            if not base:
                return {"ok": False, "detail": "no 'endpoint' in config"}
            with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=5) as resp:  # noqa: S310
                return {"ok": 200 <= resp.status < 400, "detail": f"dataplane HTTP {resp.status}"}
        if kind == "s3":
            endpoint = cfg.get("endpoint") or cfg.get("endpoint_url")
            bucket = cfg.get("bucket")
            if not bucket:
                return {"ok": False, "detail": "no 'bucket' in config"}
            try:
                import boto3  # type: ignore
            except ImportError:
                return {"ok": False, "detail": "boto3 not installed (config looks valid)"}
            try:
                secret = resolve_connection(name, project=project).get("secret")
            except Exception:
                secret = None
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=cfg.get("access_key"),
                aws_secret_access_key=secret,
            )
            s3.head_bucket(Bucket=bucket)
            return {"ok": True, "detail": f"bucket '{bucket}' reachable"}
        for connector in _connectors_accepting(kind):
            probe = connector.probe(resolve_connection(name, project=project), None)
            return {"ok": probe.ok, "detail": probe.detail}
    except Exception as exc:  # reachability failure — surface concisely, never a secret
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "detail": f"unknown kind {kind!r}"}
