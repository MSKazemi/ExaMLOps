"""Postgres backup tier — ``pg_dump`` of the MLflow + Prefect metadata databases.

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
    return TierResult("postgres", status=rollup_status([i["status"] for i in items]), items=items)


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
        rc, err = _run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", db, str(dump)], env
        )
        out.append({"name": db, "ok": rc == 0, "reason": None if rc == 0 else err.strip()[:200]})
    return out
