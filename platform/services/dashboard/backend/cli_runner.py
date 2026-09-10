"""Run `exa` commands for the CLI Console (ADR 0119).

The console's promise is "everything the CLI can do", and the only way to keep that promise as the
CLI grows is to run the CLI itself. This module is the execution half; what may run, with which
arguments, is decided before a request reaches it by :mod:`examlops.cli.surface`.

Each run is one isolated subprocess — ``python -m examlops.cli --output <fmt> --yes <argv>`` — so
a command's import-time state, ``sys.exit`` or crash never touches the dashboard process:

* **no shell**: argv is a list, built by ``surface.build_argv``;
* **finite**: a wall-clock timeout ends the whole process group (SIGTERM, then SIGKILL); stdin is
  ``/dev/null`` so a prompt fails instead of hanging;
* **bounded**: output is capped (the pipe keeps draining so the child cannot block on a full
  pipe); concurrency is capped globally and per user, and a busy runner answers 429 instead of
  queueing without limit;
* **least secret**: the child's environment drops the dashboard's own credentials (JWT signing
  key, login passwords, its database URL) — the CLI reads none of them — and names the dashboard
  user as ``EXAMLOPS_ACTOR`` so the CLI's own audit events say who acted;
* **contained**: path arguments may only name files in the CLI workspace (``surface.contain_path``)
  and reach the command as absolute paths; the command itself runs from the repo root, as an
  operator's terminal would (:func:`run_cwd`).

Run records live in memory (bounded history). The durable trace is the audit chain: the router
writes ``cli_run`` for every accepted run and ``cli_run_finished`` with its outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("dashboard.cli")

# The dashboard's own credentials. The CLI reads none of them, so a command (or a bug in one)
# must not be able to print them back.
STRIPPED_ENV = frozenset(
    {
        "DASHBOARD_JWT_SECRET",
        "DASHBOARD_VIEWER_PASSWORD",
        "DASHBOARD_ADMIN_PASSWORD",
        "DASHBOARD_AGENT_API_KEY",
        "JUPYTERHUB_DASHBOARD_TOKEN",
        "DATABASE_URL",
    }
)

TERMINAL = frozenset({"succeeded", "failed", "timeout", "cancelled", "error"})
_KILL_GRACE_SECONDS = 5.0
_CANCEL_POLL_SECONDS = 1.0


def _env_number(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def workspace_root() -> Path:
    """The one directory CLI path arguments may name (created on first use).

    ``EXAMLOPS_DASHBOARD_CLI_WORKSPACE`` wins — compose points it at a named volume so uploads and
    produced files survive a rebuild. Otherwise the user's XDG state directory
    (``$XDG_STATE_HOME/examlops/cli-workspace``, default ``~/.local/state/…``): never beside
    ``PLATFORM_DB``, which in local development is the repo root.
    """
    explicit = os.getenv("EXAMLOPS_DASHBOARD_CLI_WORKSPACE", "").strip()
    if explicit:
        root = Path(explicit)
    else:
        state = os.getenv("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
        root = Path(state) / "examlops" / "cli-workspace"
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    return root.resolve()


def run_cwd(workspace: Path) -> Path:
    """Where commands run: the repo root, as an operator runs `exa` ("run from the repo root").

    ``EXAMLOPS_DASHBOARD_CLI_CWD`` wins; then ``REPO_ROOT`` (compose sets ``/repo``); then the repo
    the ``examlops`` package is loaded from; else the workspace. Path *arguments* are unaffected —
    ``surface.build_argv`` hands them over as absolute workspace paths — so this only decides what
    a command's own relative defaults (``./backups``, ``usecases/…``) mean, exactly as a terminal.
    """
    for var in ("EXAMLOPS_DASHBOARD_CLI_CWD", "REPO_ROOT"):
        value = os.getenv(var, "").strip()
        if value and Path(value).is_dir():
            return Path(value).resolve()
    try:
        import examlops

        # <repo>/platform/cli/src/examlops/__init__.py → parents[4] is <repo>
        repo = Path(examlops.__file__).resolve().parents[4]
        if (repo / "pipelines").is_dir() and (repo / "platform").is_dir():
            return repo
    except (ImportError, IndexError, TypeError):
        pass
    return workspace


def child_env(actor: str) -> dict[str, str]:
    """The environment a CLI run sees: the dashboard's minus its credentials, plus terminal hints."""
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env.update(
        {
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "160",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "EXAMLOPS_ACTOR": actor,
        }
    )
    return env


def display_command(display_argv: list[str], fmt: str, context: str) -> str:
    """The equivalent command line an operator could paste into a terminal (secrets masked).

    The ``--`` separator is kept only when a positional starts with ``-`` — the one case where
    the terminal needs it too.
    """
    head = ["exa"]
    if fmt == "json":
        head.append("--json")
    if context:
        head += ["--context", context]
    shown = list(display_argv)
    if "--" in shown:
        cut = shown.index("--")
        if not any(a.startswith("-") for a in shown[cut + 1 :]):
            shown.pop(cut)
    return " ".join(shlex.quote(a) for a in [*head, *shown])


@dataclass
class Run:
    id: str
    command: str
    display: str
    tier: str
    fmt: str
    actor: str
    role: str
    args: dict[str, Any]
    status: str = "queued"
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    parsed: Any = None
    files: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        duration = None
        if self.started_at is not None:
            end = self.finished_at or time.time()
            duration = int((end - self.started_at) * 1000)
        return {
            "id": self.id,
            "command": self.command,
            "display": self.display,
            "tier": self.tier,
            "format": self.fmt,
            "actor": self.actor,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": duration,
            "error": self.error,
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "args": self.args,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "parsed": self.parsed,
            "files": self.files,
        }

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> Run:
        """A run as the shared store holds it; structured output is re-parsed, not stored twice."""
        run = cls(**{k: rec[k] for k in cls.__dataclass_fields__ if k in rec and k != "parsed"})
        if run.fmt == "json" and run.stdout.strip() and not run.truncated:
            with contextlib.suppress(ValueError):
                run.parsed = json.loads(run.stdout)
        return run


class Busy(Exception):
    """The runner is at its concurrency cap (maps to HTTP 429)."""


OnFinish = Callable[[Run], Awaitable[None] | None]


class CliRunner:
    """Owns the run table and the subprocesses. One instance per dashboard process."""

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        per_user: int | None = None,
        timeout: float | None = None,
        max_output: int | None = None,
        history: int = 200,
        python: str = sys.executable,
        store: Any = None,
    ) -> None:
        def pick(value: float | None, env: str, default: float) -> float:
            return value if value is not None else _env_number(env, default)

        self.max_concurrent = int(pick(max_concurrent, "EXAMLOPS_DASHBOARD_CLI_MAX_CONCURRENT", 4))
        self.per_user = int(pick(per_user, "EXAMLOPS_DASHBOARD_CLI_PER_USER", 2))
        self.timeout = pick(timeout, "EXAMLOPS_DASHBOARD_CLI_TIMEOUT", 300)
        self.max_output = int(pick(max_output, "EXAMLOPS_DASHBOARD_CLI_MAX_OUTPUT", 2_000_000))
        self.history = history
        self.python = python
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # The shared run store (`cli_store.CliRunStore`) — what lets every replica serve every
        # run and history survive a restart. None ⇒ in-memory only (a single-process runner).
        self.store = store
        self.owner = f"{os.uname().nodename}:{os.getpid()}"

    # ── bookkeeping ──────────────────────────────────────────────────────────────────────

    def _active(self, actor: str | None = None) -> int:
        return sum(
            1
            for r in self._runs.values()
            if r.status not in TERMINAL and (actor is None or r.actor == actor)
        )

    def _trim(self) -> None:
        while len(self._runs) > self.history:
            oldest = next((k for k, r in self._runs.items() if r.status in TERMINAL), None)
            if oldest is None:
                return
            self._runs.pop(oldest)

    def get(self, run_id: str) -> Run | None:
        """A run this replica owns (live), else the shared store's copy."""
        local = self._runs.get(run_id)
        if local is not None or self.store is None:
            return local
        rec = self.store.get(run_id)
        return self._reap(Run.from_record(rec)) if rec else None

    def list_runs(self, actor: str | None = None, limit: int = 50) -> list[Run]:
        if self.store is None:
            runs = [r for r in reversed(self._runs.values()) if actor is None or r.actor == actor]
            return runs[:limit]
        out = []
        for rec in self.store.list(actor=actor, limit=limit):
            local = self._runs.get(rec["id"])
            out.append(local if local is not None else self._reap(Run.from_record(rec)))
        return out

    def _reap(self, run: Run) -> Run:
        """A run no live replica owns, still 'running' long past the timeout, was lost with its
        replica (crash, redeploy). Say so instead of showing it running forever."""
        if run.status in TERMINAL or run.id in self._runs:
            return run
        since = run.started_at or run.created_at
        if time.time() - since > (self.timeout or 0) + 60:
            message = "lost: the dashboard process running it stopped before it finished"
            now = time.time()
            if self.store is not None:
                self.store.mark_lost(run.id, message, now)
            run.status, run.error, run.finished_at = "error", message, now
        return run

    async def _persist(self, run: Run) -> None:
        if self.store is None:
            return
        try:
            await asyncio.to_thread(self.store.update, run)
        except Exception:  # noqa: BLE001 — the run itself must not fail on bookkeeping
            logger.exception("cli run %s: could not persist its state", run.id)

    # ── execution ────────────────────────────────────────────────────────────────────────

    def submit(
        self,
        *,
        command: str,
        argv: list[str],
        display: str,
        tier: str,
        fmt: str,
        context: str,
        actor: str,
        role: str,
        args: dict[str, Any],
        workspace: Path,
        on_finish: OnFinish | None = None,
    ) -> Run:
        """Start a run in the background; raise :class:`Busy` at the concurrency cap."""
        if self._active() >= self.max_concurrent:
            raise Busy(f"the CLI runner is busy ({self.max_concurrent} runs in flight)")
        if self._active(actor) >= self.per_user:
            raise Busy(f"you already have {self.per_user} run(s) in flight — wait for one")
        run = Run(
            id=uuid.uuid4().hex[:16],
            command=command,
            display=display,
            tier=tier,
            fmt=fmt,
            actor=actor,
            role=role,
            args=args,
        )
        self._runs[run.id] = run
        self._trim()
        if self.store is not None:
            self.store.insert(run, owner=self.owner)
        head = [self.python, "-m", "examlops.cli", "--output", fmt, "--yes"]
        if context:
            head += ["--context", context]
        task = asyncio.create_task(self._execute(run, [*head, *argv], workspace, on_finish))
        self._tasks[run.id] = task
        task.add_done_callback(self._forget(run.id))
        return run

    def _forget(self, run_id: str) -> Callable[[asyncio.Task[None]], None]:
        """A done-callback that drops the finished task's reference (the run record stays)."""

        def done(_task: asyncio.Task[None]) -> None:
            self._tasks.pop(run_id, None)

        return done

    async def _execute(
        self, run: Run, full: list[str], workspace: Path, on_finish: OnFinish | None
    ) -> None:
        before = _snapshot(workspace)
        run.status = "running"
        run.started_at = time.time()
        await self._persist(run)
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                cwd=str(run_cwd(workspace)),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(run.actor),
                start_new_session=True,
            )
        except OSError as exc:
            run.status, run.error, run.finished_at = "error", f"could not start: {exc}", time.time()
            await self._persist(run)
            await _notify(on_finish, run)
            return

        self._procs[run.id] = proc
        out_buf, err_buf = _Capped(self.max_output), _Capped(max(self.max_output // 4, 1))
        readers = asyncio.gather(_drain(proc.stdout, out_buf), _drain(proc.stderr, err_buf))
        watcher = asyncio.create_task(self._watch_cancel(run, proc))
        try:
            await asyncio.wait_for(asyncio.shield(readers), timeout=self.timeout or None)
        except TimeoutError:
            run.status = "timeout"
            run.error = f"stopped after {self.timeout:g}s (EXAMLOPS_DASHBOARD_CLI_TIMEOUT)"
        finally:
            watcher.cancel()
            await _stop(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(readers, timeout=_KILL_GRACE_SECONDS)
            self._procs.pop(run.id, None)

        run.exit_code = proc.returncode
        run.stdout, run.stderr = out_buf.text(), err_buf.text()
        run.truncated = out_buf.truncated or err_buf.truncated
        if run.status == "running":
            run.status = "succeeded" if proc.returncode == 0 else "failed"
        if run.fmt == "json" and run.stdout.strip() and not out_buf.truncated:
            with contextlib.suppress(ValueError):
                run.parsed = json.loads(run.stdout)
        run.files = sorted(_snapshot(workspace, changed_since=before))
        run.finished_at = time.time()
        await self._persist(run)
        await _notify(on_finish, run)

    async def _watch_cancel(self, run: Run, proc: asyncio.subprocess.Process) -> None:
        """While the process runs, honour a cancel requested on *another* replica (the flag)."""
        if self.store is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            while proc.returncode is None:
                await asyncio.sleep(_CANCEL_POLL_SECONDS)
                if await asyncio.to_thread(self.store.cancel_requested, run.id):
                    run.status, run.error = "cancelled", "cancelled by user"
                    _signal(proc, signal.SIGTERM)
                    return

    async def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is not None:
            if run.status in TERMINAL:
                return False
            run.status, run.error = "cancelled", "cancelled by user"
            proc = self._procs.get(run_id)
            if proc is not None:
                _signal(proc, signal.SIGTERM)
            return True
        # Another replica owns it: raise the flag its watcher polls.
        if self.store is None:
            return False
        return bool(await asyncio.to_thread(self.store.request_cancel, run_id))

    async def wait(self, run_id: str, timeout: float = 60) -> Run | None:
        """Await a run's completion (tests and synchronous callers)."""
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self._runs.get(run_id)


class _Capped:
    """A byte buffer that keeps the first ``limit`` bytes and remembers it dropped the rest."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[bytes] = []
        self.size = 0
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        room = self.limit - self.size
        if room <= 0:
            self.truncated = True
            return
        if len(chunk) > room:
            chunk, self.truncated = chunk[:room], True
        self.parts.append(chunk)
        self.size += len(chunk)

    def text(self) -> str:
        return b"".join(self.parts).decode("utf-8", errors="replace")


async def _drain(stream: asyncio.StreamReader | None, buf: _Capped) -> None:
    # Keep reading past the cap: a child writing into a full, unread pipe would block forever.
    if stream is None:
        return
    while chunk := await stream.read(65536):
        buf.add(chunk)


def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Make sure the run's whole process group is gone: SIGTERM, a grace period, then SIGKILL."""
    if proc.returncode is not None:
        return
    _signal(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    except TimeoutError:
        _signal(proc, signal.SIGKILL)
        await proc.wait()


def _snapshot(root: Path, changed_since: dict[str, float] | None = None) -> dict[str, float]:
    """Workspace files → mtime; with ``changed_since``, only files new or modified since then."""
    found: dict[str, float] = {}
    try:
        for i, path in enumerate(root.rglob("*")):
            if i > 5000:
                break
            if path.is_file() and not path.is_symlink():
                rel = str(path.relative_to(root))
                mtime = path.stat().st_mtime
                if changed_since is None or changed_since.get(rel) != mtime:
                    found[rel] = mtime
    except OSError:
        pass
    return found


async def _notify(callback: OnFinish | None, run: Run) -> None:
    if callback is None:
        return
    try:
        result = callback(run)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 — a failed notification must not lose the run record
        logger.exception("cli run %s: completion hook failed", run.id)


def _default_store() -> Any:
    from cli_store import CliRunStore

    return CliRunStore()


RUNNER = CliRunner(store=_default_store())
