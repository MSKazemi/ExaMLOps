"""Postgres backup tier — ``pg_dump`` of the MLflow + Prefect metadata databases, and of the
**platform datastore itself** when ``EXAMLOPS_DB_BACKEND=postgres``.

That last part is the one that makes a backup honest. With the Postgres engine selected, platform
state — the audit chain, the registry, every helper's table — lives in Postgres, and the SQLite
tier's ``platform.db`` is an empty leftover file. Backing that file up produces a bundle that looks
complete and restores nothing.

Uses the ``pg_dump`` binary in **custom format** (``-Fc``: compressed, selectively restorable,
parallelisable) over TCP — in the Compose sidecar this reaches the ``postgres`` service directly
(no docker socket needed). Restore is ``pg_restore --clean --if-exists`` and is guarded behind
``--force`` because it drops objects.

Degradation (the platform convention): ``pg_dump`` missing from PATH, or Postgres unreachable,
raises :class:`TierUnavailable` → the tier is ``skipped`` and the bundle still succeeds. Tests never
need a live Postgres: they monkeypatch the :func:`_run` / :func:`shutil.which` seams.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ._manifest import FAILED, OK, TierResult, TierUnavailable, rollup_status, sha256_file

# stderr fragments that mean "the server isn't reachable" (→ skip, not fail).
_CONN_ERROR_MARKERS = (
    "could not connect",
    "connection refused",
    "could not translate host",
    "timeout expired",
    "no route to host",
    "could not receive data",
    "server closed the connection",
    "password authentication failed",
)


def _pg_databases() -> list[str]:
    raw = os.getenv("EXAMLOPS_BACKUP_PG_DBS", "mlflow,prefect")
    return [d.strip() for d in raw.split(",") if d.strip()]


def _first(*vals: str | None, default: str) -> str:
    for v in vals:
        if v:
            return v
    return default


def _pg_env() -> dict[str, str]:
    """Subprocess env carrying libpq connection params (password via env, never argv)."""
    env = dict(os.environ)
    env["PGHOST"] = os.getenv("PGHOST", "localhost")
    env["PGPORT"] = os.getenv("PGPORT", "5432")
    env["PGUSER"] = _first(os.getenv("PGUSER"), os.getenv("POSTGRES_USER"), default="mlops")
    env["PGPASSWORD"] = _first(
        os.getenv("PGPASSWORD"), os.getenv("POSTGRES_PASSWORD"), default="mlops"
    )
    return env


def platform_dsn() -> str | None:
    """The platform datastore's DSN, or ``None`` when the engine is SQLite.

    Public because the SQLite tier asks the same question: exactly one of the two tiers owns
    platform state, and neither may assume it.
    """
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres":
        return None
    return os.getenv("EXAMLOPS_POSTGRES_DSN", "").strip() or None


def _dsn_env(dsn: str) -> dict[str, str]:
    """libpq environment for a DSN — the password goes in the env, never in ``argv``.

    ``pg_dump -d postgresql://user:pw@host/db`` puts the password in the process table, where any
    user on the box can read it with ``ps``. Splitting the URI into ``PG*`` variables keeps the
    credential out of every process listing, which is also how the tier already handles the
    MLflow/Prefect databases.
    """
    parts = urlsplit(dsn)
    env = dict(os.environ)
    env.pop("PGSERVICE", None)  # a stale service file would silently win over these
    if parts.hostname:
        env["PGHOST"] = parts.hostname
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    database = parts.path.lstrip("/")
    if database:
        env["PGDATABASE"] = database
    return env


def _run(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run a subprocess, returning ``(returncode, stderr)``. Seam for tests to monkeypatch."""
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    return proc.returncode, proc.stderr


def _is_conn_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in _CONN_ERROR_MARKERS)


def backup_postgres_tier(dest_dir: Path) -> TierResult:
    """Dump each configured Postgres DB into ``dest_dir/postgres/<db>.dump``."""
    if shutil.which("pg_dump") is None:
        raise TierUnavailable("pg_dump not on PATH (install postgresql-client)")
    pg_dir = dest_dir / "postgres"
    pg_dir.mkdir(parents=True, exist_ok=True)
    env = _pg_env()
    items: list[dict[str, Any]] = []
    for db in _pg_databases():
        out = pg_dir / f"{db}.dump"
        rc, err = _run(["pg_dump", "-Fc", "-d", db, "-f", str(out)], env)
        if rc != 0:
            if _is_conn_error(err):
                # A whole-tier connectivity problem → degrade the entire tier.
                raise TierUnavailable(f"postgres unreachable: {err.strip()[:160]}")
            items.append(
                {"name": db, "status": FAILED, "reason": err.strip()[:200] or f"pg_dump rc={rc}"}
            )
            continue
        items.append(
            {
                "name": db,
                "file": f"postgres/{out.name}",
                "sha256": sha256_file(out),
                "size_bytes": out.stat().st_size,
                "format": "pg_dump-custom-v1",
                "status": OK,
            }
        )
    items.extend(_backup_platform_db(pg_dir))
    return TierResult("postgres", status=rollup_status([i["status"] for i in items]), items=items)


def _backup_platform_db(pg_dir: Path) -> list[dict[str, Any]]:
    """Dump the platform datastore when Postgres is the configured engine.

    Scoped to ``EXAMLOPS_POSTGRES_SCHEMA`` when set, because that variable is what makes one
    database hold several independent platform instances — dumping the whole database would mix
    them, and restoring it would overwrite tenants that were never part of this backup.
    """
    dsn = platform_dsn()
    if not dsn:
        return []
    out = pg_dir / "platform.dump"
    schema = os.getenv("EXAMLOPS_POSTGRES_SCHEMA", "").strip()
    cmd = ["pg_dump", "-Fc", "-f", str(out)]
    if schema:
        cmd += ["--schema", schema]
    rc, err = _run(cmd, _dsn_env(dsn))
    if rc != 0:
        if _is_conn_error(err):
            raise TierUnavailable(f"platform datastore unreachable: {err.strip()[:160]}")
        return [
            {
                "name": "platform",
                "status": FAILED,
                "reason": err.strip()[:200] or f"pg_dump rc={rc}",
            }
        ]
    return [
        {
            "name": "platform",
            "file": f"postgres/{out.name}",
            "sha256": sha256_file(out),
            "size_bytes": out.stat().st_size,
            "format": "pg_dump-custom-v1",
            "dsn_env": "EXAMLOPS_POSTGRES_DSN",  # restore reconnects from the env, not from argv
            "schema": schema or None,
            "status": OK,
        }
    ]


def restore_postgres_tier(bundle_dir: Path, *, force: bool = False) -> list[dict[str, Any]]:
    """Restore each dumped DB via ``pg_restore --clean --if-exists`` (requires ``force``)."""
    import json

    if not force:
        raise ValueError("postgres restore drops+recreates objects — pass force=True to proceed")
    if shutil.which("pg_restore") is None:
        raise TierUnavailable("pg_restore not on PATH (install postgresql-client)")
    manifest = json.loads((bundle_dir / "bundle.manifest.json").read_text())
    env = _pg_env()
    out: list[dict[str, Any]] = []
    for item in manifest.get("tiers", {}).get("postgres", {}).get("items", []):
        if item.get("status") != OK:
            continue
        db = item["name"]
        dump = bundle_dir / item["file"]
        cmd = ["pg_restore", "--clean", "--if-exists", "--no-owner"]
        if item.get("dsn_env"):
            # The platform datastore: reconnect from the *current* DSN, not from whatever database
            # name the dump was taken against, so a restore into a standby is a config change.
            dsn = platform_dsn()
            if not dsn:
                out.append(
                    {
                        "name": db,
                        "ok": False,
                        "reason": "EXAMLOPS_DB_BACKEND=postgres + EXAMLOPS_POSTGRES_DSN required",
                    }
                )
                continue
            # `pg_restore` will not take its target from PGDATABASE — it requires -d (or -f) and
            # exits 1 saying so. The database *name* is not a credential, so naming it in argv is
            # safe; the password still travels in the environment.
            env_dsn = _dsn_env(dsn)
            target = env_dsn.get("PGDATABASE")
            if not target:
                out.append({"name": db, "ok": False, "reason": f"no database in {dsn!r}"})
                continue
            rc, err = _run([*cmd, "-d", target, str(dump)], env_dsn)
        else:
            rc, err = _run([*cmd, "-d", db, str(dump)], env)
        out.append({"name": db, "ok": rc == 0, "reason": None if rc == 0 else err.strip()[:200]})
    return out
