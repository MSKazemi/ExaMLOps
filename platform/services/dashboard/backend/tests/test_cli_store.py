"""CLI Console runs live in the shared datastore, so any replica can serve them (ADR 0119).

The Helm chart runs the dashboard with two replicas behind one Service. An in-memory run table
meant a run started on replica A was a 404 when its poll landed on replica B, and a restart lost
the history. Runs are now rows in `platform.db`: the owning replica writes them, every replica
reads them, and cancellation is a flag the owner watches.
"""

from __future__ import annotations

import asyncio
import sys
import time

import cli_runner
import cli_store
import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    return str(tmp_path / "platform.db")


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def _submit(runner, tmp_path, command="x", actor="dashboard:admin@a"):
    return runner.submit(
        command=command,
        argv=[],
        display=f"exa {command}",
        tier="read",
        fmt="json",
        context="",
        actor=actor,
        role="admin",
        args={"k": "v"},
        workspace=tmp_path,
    )


async def test_a_run_started_on_one_replica_is_readable_from_another(db, tmp_path):
    echo = _script(tmp_path, "echo.sh", 'echo \'[{"name": "p1"}]\'')
    replica_a = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=echo)
    replica_b = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=echo)
    run = _submit(replica_a, tmp_path, "project list")
    await replica_a.wait(run.id)
    seen = replica_b.get(run.id)
    assert seen is not None
    assert seen.status == "succeeded"
    assert seen.parsed == [{"name": "p1"}]  # re-derived from the stored stdout
    assert seen.args == {"k": "v"}
    assert [r.id for r in replica_b.list_runs()] == [run.id]


async def test_history_survives_a_restart(db, tmp_path):
    echo = _script(tmp_path, "echo.sh", "echo '{}'")
    first = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=echo)
    run = _submit(first, tmp_path)
    await first.wait(run.id)
    after_restart = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=echo)
    assert after_restart.get(run.id).status == "succeeded"


async def test_cancel_from_another_replica_stops_the_owners_process(db, tmp_path):
    sleeper = _script(tmp_path, "sleep.sh", "exec sleep 30")
    owner = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=sleeper, timeout=60)
    other = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=sleeper, timeout=60)
    run = _submit(owner, tmp_path)
    await asyncio.sleep(0.3)
    assert await other.cancel(run.id) is True  # not this replica's process
    t0 = time.monotonic()
    done = await owner.wait(run.id, timeout=20)
    assert done.status == "cancelled"
    assert time.monotonic() - t0 < 10  # the owner noticed the flag, not the 60 s timeout


async def test_a_run_lost_with_its_replica_is_reported_not_left_running(db, tmp_path):
    store = cli_store.CliRunStore()
    stale = cli_runner.Run(
        id="lost1",
        command="x",
        display="exa x",
        tier="read",
        fmt="json",
        actor="a",
        role="admin",
        args={},
        status="running",
        started_at=time.time() - 3600,
    )
    await asyncio.to_thread(store.insert, stale, owner="gone-host:1")
    runner = cli_runner.CliRunner(store=store, timeout=60)
    seen = runner.get("lost1")
    assert seen.status == "error"
    assert "lost" in (seen.error or "")


async def test_stored_output_is_capped_and_history_is_bounded(db, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DASHBOARD_CLI_STORE_OUTPUT", "100")
    monkeypatch.setenv("EXAMLOPS_DASHBOARD_CLI_HISTORY", "3")
    loud = _script(tmp_path, "loud.sh", f"{sys.executable} -c \"print('x' * 5000)\"")
    runner = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=loud)
    ids = []
    for _ in range(5):
        run = _submit(runner, tmp_path)
        await runner.wait(run.id)
        ids.append(run.id)
    fresh = cli_runner.CliRunner(store=cli_store.CliRunStore(), python=loud)
    kept = [r.id for r in fresh.list_runs(limit=50)]
    assert kept == list(reversed(ids[-3:]))
    stored = fresh.get(ids[-1])
    assert len(stored.stdout) <= 100 and stored.truncated is True
