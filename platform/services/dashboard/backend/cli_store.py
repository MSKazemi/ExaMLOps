"""Shared store for CLI Console runs (ADR 0119) — any dashboard replica can serve any run.

The dashboard runs with several replicas behind one Service (the Helm chart defaults to two), so
the run a user started on replica A may be polled on replica B, and a restart must not erase
history. Runs are rows in the platform datastore (`dashboard_cli_runs`, an additive,
dashboard-owned table) reached through `dbconn.connect`, so the same code serves SQLite and the
Postgres backend.

* The replica that owns the subprocess writes every transition; readers never write — except to
  mark a run whose owner vanished (``status='running'`` long past the timeout) as lost.
* Cancellation is a flag (``cancel_requested``) the owner polls while the process runs.
* Stored stdout is capped (``EXAMLOPS_DASHBOARD_CLI_STORE_OUTPUT``); structured output is
  re-parsed on read rather than stored twice. History keeps the newest
  ``EXAMLOPS_DASHBOARD_CLI_HISTORY`` runs.
"""

from __future__ import annotations

import json
import os
import socket
from typing import TYPE_CHECKING, Any

from dbconn import connect, platform_db_path

if TYPE_CHECKING:  # pragma: no cover
    from cli_runner import Run

_COLUMNS = (
    "id",
    "command",
    "display",
    "tier",
    "fmt",
    "actor",
    "role",
    "args",
    "status",
    "exit_code",
    "created_at",
    "started_at",
    "finished_at",
    "stdout",
    "stderr",
    "truncated",
    "files",
    "error",
    "owner",
    "cancel_requested",
)


def owner_id() -> str:
    """This replica's identity for the runs it owns: host plus process."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class CliRunStore:
    """Rows in `dashboard_cli_runs`. Blocking calls — the runner uses them off the event loop."""

    def _connect(self) -> Any:
        conn = connect(platform_db_path(), row_factory=None)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_cli_runs ("
            " id TEXT PRIMARY KEY, command TEXT NOT NULL, display TEXT, tier TEXT, fmt TEXT,"
            " actor TEXT, role TEXT, args TEXT, status TEXT NOT NULL, exit_code INTEGER,"
            " created_at REAL, started_at REAL, finished_at REAL, stdout TEXT, stderr TEXT,"
            " truncated INTEGER DEFAULT 0, files TEXT, error TEXT, owner TEXT,"
            " cancel_requested INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS dashboard_cli_runs_created ON dashboard_cli_runs (created_at)"
        )
        return conn

    @staticmethod
    def _cap(text: str) -> tuple[str, bool]:
        limit = _env_int("EXAMLOPS_DASHBOARD_CLI_STORE_OUTPUT", 256_000)
        return (text[:limit], True) if len(text) > limit else (text, False)

    def insert(self, run: Run, *, owner: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO dashboard_cli_runs (id, command, display, tier, fmt, actor, role,"
                " args, status, exit_code, created_at, started_at, finished_at, stdout, stderr,"
                " truncated, files, error, owner, cancel_requested)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    run.id,
                    run.command,
                    run.display,
                    run.tier,
                    run.fmt,
                    run.actor,
                    run.role,
                    json.dumps(run.args),
                    run.status,
                    run.exit_code,
                    run.created_at,
                    run.started_at,
                    run.finished_at,
                    "",
                    "",
                    0,
                    "[]",
                    run.error,
                    owner,
                ),
            )
            self._prune(conn)
            conn.commit()
        finally:
            conn.close()

    def update(self, run: Run) -> None:
        stdout, cut_out = self._cap(run.stdout)
        stderr, cut_err = self._cap(run.stderr)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE dashboard_cli_runs SET status=?, exit_code=?, started_at=?, finished_at=?,"
                " stdout=?, stderr=?, truncated=?, files=?, error=? WHERE id=?",
                (
                    run.status,
                    run.exit_code,
                    run.started_at,
                    run.finished_at,
                    stdout,
                    stderr,
                    int(run.truncated or cut_out or cut_err),
                    json.dumps(run.files),
                    run.error,
                    run.id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def update_live(self, run_id: str, *, stdout: str, stderr: str, truncated: bool) -> None:
        """A running command's output so far. Touches only the output columns, and only while the
        row is still in flight: the owner's last partial write can land after its final one (it
        runs in a thread the event loop cannot cancel), and must never turn a finished run back
        into a partial one."""
        out, cut_out = self._cap(stdout)
        err, cut_err = self._cap(stderr)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE dashboard_cli_runs SET stdout=?, stderr=?, truncated=?"
                " WHERE id=? AND status IN ('queued', 'running')",
                (out, err, int(truncated or cut_out or cut_err), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM dashboard_cli_runs WHERE id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._row(row) if row else None

    def list(self, actor: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            sql = f"SELECT {', '.join(_COLUMNS)} FROM dashboard_cli_runs"
            params: tuple[Any, ...] = ()
            if actor is not None:
                sql += " WHERE actor=?"
                params = (actor,)
            sql += " ORDER BY created_at DESC LIMIT ?"
            rows = conn.execute(sql, (*params, limit)).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def request_cancel(self, run_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE dashboard_cli_runs SET cancel_requested=1"
                " WHERE id=? AND status IN ('queued', 'running')",
                (run_id,),
            )
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()

    def cancel_requested(self, run_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT cancel_requested FROM dashboard_cli_runs WHERE id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])

    def mark_lost(self, run_id: str, message: str, at: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE dashboard_cli_runs SET status='error', error=?, finished_at=?"
                " WHERE id=? AND status IN ('queued', 'running')",
                (message, at, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _prune(self, conn: Any) -> None:
        keep = _env_int("EXAMLOPS_DASHBOARD_CLI_HISTORY", 500)
        conn.execute(
            "DELETE FROM dashboard_cli_runs WHERE id NOT IN ("
            " SELECT id FROM dashboard_cli_runs ORDER BY created_at DESC LIMIT ?)",
            (keep,),
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        rec = dict(zip(_COLUMNS, row, strict=True))
        rec["args"] = json.loads(rec["args"] or "{}")
        rec["files"] = json.loads(rec["files"] or "[]")
        rec["truncated"] = bool(rec["truncated"])
        rec["cancel_requested"] = bool(rec["cancel_requested"])
        return rec
