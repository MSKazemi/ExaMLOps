"""Running the agent runtime as a process (ADR 0144 decisions 3-5).

:class:`~examlops.agent_runtime.runtime.AgentRuntime` is a library: it steps runs when asked.
A *deployed* runtime also has to do three things on its own, and this module is where they live:

* **Follow the snapshot** (decision 5, static stability). The control plane compiles the agent
  snapshot (``exa agent runtime snapshot --out``) and a file carries it to the serving plane. The
  maintainer adopts a newer generation when the file changes; a file that is unreadable, invalid,
  tampered or older is **refused and the last-known-good snapshot stays in force** - a broken
  control plane can stop new configuration from arriving, never take running agents down. The
  refusal is counted and logged, because deliberate staleness needs a signal.
* **Move quiet sessions down the lifecycle** (decision 4): ``active -> idle -> suspended``, which
  releases each suspended session's sandbox and its admission slot (``AgentRuntime.sweep``).
* **Recover orphaned runs** (decision 3): a run left ``running`` by a worker that died, whose
  lease has expired, is resumed from its last checkpoint by this worker (``AgentRuntime.recover``);
  derived idempotency keys make the re-executed node's tool calls safe.

Every pass is bounded (one snapshot read, one sweep, at most ``recover_limit`` recovered runs -
each of which still runs to its own step budget, inline) and never raises: a failure of
one duty is recorded in :meth:`RuntimeMaintainer.status` and does not stop the others.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from examlops.agent_runtime.runtime import AgentRuntime
from examlops.agent_runtime.sandbox import SandboxProvider
from examlops.agent_runtime.snapshot import load_snapshot_file
from examlops.agent_runtime.store import AgentStateStore

__all__ = [
    "DEFAULT_INTERVAL",
    "RuntimeMaintainer",
    "build_runtime",
    "check_bind",
    "detect_sandbox_providers",
    "serve",
    "snapshot_path_from_env",
]

logger = logging.getLogger(__name__)

#: Seconds between maintenance passes (`exa agent runtime serve --interval`, env
#: ``EXAMLOPS_AGENT_RUNTIME_INTERVAL``).
DEFAULT_INTERVAL = 15.0
_MIN_INTERVAL = 0.05
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def snapshot_path_from_env() -> Path | None:
    """``EXAMLOPS_AGENT_SNAPSHOT`` (the file ``exa agent runtime snapshot --out`` writes)."""
    raw = os.getenv("EXAMLOPS_AGENT_SNAPSHOT", "").strip()
    return Path(raw).expanduser() if raw else None


class RuntimeMaintainer:
    """The background duties of a deployed agent runtime. See the module docstring."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        snapshot_path: str | Path | None = None,
        interval: float = DEFAULT_INTERVAL,
        recover: bool = True,
        recover_limit: int = 5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not interval or interval < _MIN_INTERVAL:
            raise ValueError(f"maintenance interval must be at least {_MIN_INTERVAL}s")
        self.runtime = runtime
        self.snapshot_path = Path(snapshot_path).expanduser() if snapshot_path else None
        self.interval = float(interval)
        self.recover_enabled = recover
        #: Orphaned runs resumed per pass; the rest wait for the next pass, so one pass cannot
        #: turn into an unbounded drain that starves the snapshot follow and the sweep.
        self.recover_limit = max(1, int(recover_limit))
        self.clock = clock
        self._seen: tuple[int, int] | None = None  # (mtime_ns, size) of the last file we read
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "passes": 0,
            "last_pass_at": None,
            "snapshot": {
                "path": str(self.snapshot_path) if self.snapshot_path else None,
                "generation": (runtime.snapshot or {}).get("generation"),
                "adopted": 0,
                "rejected": 0,
                "stale": 0,
                "last_error": None,
            },
            "sweep": {"idle": 0, "suspended": 0, "last_error": None},
            "recovery": {"recovered": 0, "last_error": None},
        }

    # -- one pass ---------------------------------------------------------------------------------

    def _follow_snapshot(self) -> None:
        snap = self._status["snapshot"]
        if self.snapshot_path is None:
            return
        try:
            st = self.snapshot_path.stat()
        except FileNotFoundError:
            if self.runtime.snapshot is None:
                snap["last_error"] = f"{self.snapshot_path} does not exist yet"
            return
        except OSError as exc:
            snap["last_error"] = f"cannot stat {self.snapshot_path}: {exc}"
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._seen:
            return
        self._seen = sig
        try:
            doc = load_snapshot_file(self.snapshot_path)
            adopted = self.runtime.apply_snapshot(doc)
        except Exception as exc:  # noqa: BLE001 - a pass never raises; any bad file is refused
            snap["rejected"] += 1
            snap["last_error"] = f"snapshot refused, keeping last-known-good: {exc}"
            logger.warning(
                "agent snapshot %s refused; generation %s stays in force: %s",
                self.snapshot_path,
                (self.runtime.snapshot or {}).get("generation"),
                exc,
            )
            return
        if adopted:
            snap["adopted"] += 1
            snap["generation"] = doc.get("generation")
            snap["last_error"] = None
            logger.info("agent snapshot generation %s adopted", doc.get("generation"))
        else:
            snap["stale"] += 1
            snap["last_error"] = (
                f"snapshot generation {doc.get('generation')} is older than the one in force "
                f"({(self.runtime.snapshot or {}).get('generation')}); ignored"
            )

    def _sweep(self) -> None:
        sw = self._status["sweep"]
        try:
            moved = self.runtime.sweep()
        except Exception as exc:  # noqa: BLE001 - one duty failing must not stop the others
            sw["last_error"] = str(exc)
            logger.warning("agent session sweep failed: %s", exc)
            return
        sw["idle"] += len(moved.get("idle", []))
        sw["suspended"] += len(moved.get("suspended", []))
        sw["last_error"] = None

    def _recover(self) -> None:
        rec = self._status["recovery"]
        if not self.recover_enabled or self.runtime.snapshot is None:
            return
        try:
            done = self.runtime.recover(limit=self.recover_limit)
        except Exception as exc:  # noqa: BLE001 - see _sweep
            rec["last_error"] = str(exc)
            logger.warning("agent run recovery failed: %s", exc)
            return
        rec["recovered"] += len(done)
        rec["last_error"] = None

    def tick(self) -> dict[str, Any]:
        """Run one maintenance pass now; returns :meth:`status`. Never raises."""
        with self._lock:
            self._follow_snapshot()
            self._sweep()
            self._recover()
            self._status["passes"] += 1
            self._status["last_pass_at"] = self.clock()
        return self.status()

    # -- background loop --------------------------------------------------------------------------

    def start(self) -> None:
        """Start the background loop (idempotent). The first pass runs immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agent-runtime-maintainer", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        import copy

        out = copy.deepcopy(self._status)
        out["running"] = self.running
        out["interval"] = self.interval
        out["snapshot"]["generation"] = (self.runtime.snapshot or {}).get("generation")
        return out


def build_runtime(
    *,
    snapshot_path: str | Path | None = None,
    state_db: str | None = None,
    worker_id: str | None = None,
    peers: Sequence[str] = (),
) -> AgentRuntime:
    """An :class:`AgentRuntime` over the serving-plane state store and the snapshot file.

    The initial snapshot is the file when it is valid, else the last-known-good copy the store
    kept. With neither, the runtime still starts and answers ``no_snapshot`` (503) until one
    arrives - it never reads ``platform.db`` to make one up (ADR 0144 d5).
    """
    store = AgentStateStore(state_db)
    initial: dict[str, Any] | None = None
    path = Path(snapshot_path).expanduser() if snapshot_path else None
    if path is not None and path.exists():
        try:
            initial = load_snapshot_file(path)
        except (OSError, ValueError) as exc:
            logger.warning("agent snapshot %s refused at start: %s", path, exc)
    wid = worker_id or os.getenv("EXAMLOPS_AGENT_RUNTIME_WORKER", "").strip() or "worker-1"
    return AgentRuntime(
        store,
        snapshot=initial,
        worker_id=wid,
        peers=tuple(peers),
        sandbox_providers=detect_sandbox_providers(),
    )


def detect_sandbox_providers(
    which: Callable[[str], str | None] | None = None,
) -> list[SandboxProvider]:
    """The sandbox providers whose runtime binary is installed on this host.

    Presence only: each provider still measures its own isolation (Docker asks the daemon whether
    ``runsc`` exists) and the runtime refuses a tenant whose policy needs more than the strongest
    one offers. None installed means every code-executing call is refused, never run unsandboxed.
    """
    import shutil

    from examlops.agent_runtime.sandbox import ApptainerProvider, DockerProvider

    look = which or shutil.which
    found: list[SandboxProvider] = []
    if look("docker"):
        found.append(DockerProvider())
    if look("apptainer"):
        found.append(ApptainerProvider())
    return found


def check_bind(host: str, *, allow_remote: bool) -> None:
    """``ValueError`` unless ``host`` is loopback or ``allow_remote`` was given (fail closed)."""
    if host.strip().lower() not in _LOOPBACK and not allow_remote:
        raise ValueError(
            f"refusing to bind {host}: the agent runtime listens on loopback unless "
            "--allow-remote is given (put it behind the platform's TLS ingress)"
        )


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 18005,
    snapshot_path: str | Path | None = None,
    state_db: str | None = None,
    worker_id: str | None = None,
    peers: Sequence[str] = (),
    interval: float = DEFAULT_INTERVAL,
    background_workers: int = 4,
    allow_remote: bool = False,
) -> None:  # pragma: no cover - exercised through the parts above; uvicorn blocks
    """Run the agent runtime's HTTP surface with its maintainer until interrupted.

    Binding beyond loopback needs ``allow_remote``: the surface's own bearer tokens
    (``EXAMLOPS_AGENT_RUNTIME_TOKENS``) are the only authentication in front of it.
    """
    check_bind(host, allow_remote=allow_remote)
    import uvicorn

    from examlops.agent_runtime.http import create_app

    runtime = build_runtime(
        snapshot_path=snapshot_path, state_db=state_db, worker_id=worker_id, peers=peers
    )
    maintainer = RuntimeMaintainer(runtime, snapshot_path=snapshot_path, interval=interval)
    app = create_app(runtime, background_workers=background_workers, maintainer=maintainer)
    maintainer.start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        maintainer.stop()
