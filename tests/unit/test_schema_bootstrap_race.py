"""Two processes opening a new platform database at once both finish its schema (SQLite).

Two races, both seen as a control-plane retrain submission answering 503 ("Shared rate limiter
unavailable: …") when two requests reached a new database together:

- The additive column migrations check ``PRAGMA table_info`` and then ``ALTER TABLE … ADD
  COLUMN``. On SQLite those were two autocommit statements, so two starters could both see a
  column missing and the second failed with ``duplicate column name``. They now run under
  SQLite's write lock; Postgres was already serialised by an advisory lock.
- Switching a new file to WAL needs an exclusive lock, and SQLite refuses it with "database is
  locked" without calling the busy handler, so all openers but one failed at once.
  ``resilience.db`` now retries the switch within the busy timeout.
- A statement of the schema script can fail with "database schema has changed" when another
  process commits DDL between its preparation and its execution. The script is all
  ``IF NOT EXISTS``, so the bootstrap runs it again (2 failures in 18 loaded runs without that).
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path


def _open(db: str, go, results) -> None:
    os.environ["PLATFORM_DB"] = db
    os.environ.pop("EXAMLOPS_DB_BACKEND", None)
    from examlops import platform_db

    go.wait(10)
    try:
        platform_db.init_db()
        results.put("ok")
    except Exception as exc:  # noqa: BLE001 - reported to the test
        results.put(f"{type(exc).__name__}: {exc}")


def test_concurrent_first_opens_all_succeed(tmp_path: Path, monkeypatch):
    # A spawned child re-imports this module (tests.unit.…) by the parent's sys.path. Under xdist a
    # test that ran earlier in the same worker can leave the repository root off it, and every
    # child then died with "No module named 'tests.unit'" before opening anything.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    ctx = mp.get_context("spawn")
    failures: list[str] = []
    for round_ in range(4):  # the race is timing-dependent; several fresh databases
        db = str(tmp_path / f"fresh-{round_}.db")
        go, results = ctx.Event(), ctx.Queue()
        procs = [ctx.Process(target=_open, args=(db, go, results)) for _ in range(6)]
        for p in procs:
            p.start()
        go.set()
        for p in procs:
            p.join(60)
        outcomes = [results.get(timeout=5) for _ in procs]
        failures += [o for o in outcomes if o != "ok"]
    assert not failures, failures


def test_concurrent_first_opens_in_one_process_all_succeed(tmp_path: Path, monkeypatch):
    """Threads of one process (the control plane's request threads) opening a new database.
    One bootstraps it under platform_db's per-process lock and the rest wait. Without that lock
    this failed about one run in five with "database schema has changed"."""
    import threading

    from examlops import platform_db

    failures: list[str] = []
    for round_ in range(8):
        monkeypatch.setenv("PLATFORM_DB", str(tmp_path / f"threads-{round_}.db"))
        barrier = threading.Barrier(8)

        def open_it() -> None:
            barrier.wait(10)
            try:
                platform_db.init_db()
            except Exception as exc:  # noqa: BLE001 - reported to the test
                failures.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    assert not failures, failures
