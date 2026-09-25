"""A deployed agent runtime's own duties (ADR 0144 d3-d5): follow the snapshot file (keeping the
last-known-good on a bad one), sweep the session lifecycle, recover orphaned runs; and the
``exa agent runtime serve`` entrypoint's fail-closed refusals.

Real runtime, real SQLite agent state store, real snapshot files; every test asserts an outcome.
"""

from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from examlops.agent_runtime import AgentStateStore, Node
from examlops.agent_runtime.service import (
    RuntimeMaintainer,
    build_runtime,
    check_bind,
    snapshot_path_from_env,
)
from examlops.agent_runtime.snapshot import write_snapshot
from tests.unit._agent_runtime_fixtures import (
    linear_program,
    make_runtime,
    manifest,
    snapshot,
)


def _prog():
    def answer(state, ctx):
        return {"output": f"ok:{state['input']}"}

    return linear_program(Node("answer", answer))


def _rt(tmp_path, snap, **kw):
    return make_runtime(tmp_path, snap, {"tests.jobdoc:program": _prog()}, **kw)


def _bump(path):
    """Make a rewrite visible to an mtime check even on a coarse-grained filesystem clock."""
    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


# -- following the snapshot -------------------------------------------------------------------


def test_a_newer_snapshot_file_is_adopted_once(tmp_path):
    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=1))
    path = tmp_path / "agent-snapshot.json"
    write_snapshot(snapshot([m], generation=2), path)
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)

    st = mt.tick()
    assert rt.snapshot["generation"] == 2
    assert st["snapshot"]["adopted"] == 1 and st["snapshot"]["last_error"] is None
    mt.tick()  # unchanged file: not re-read, not re-counted
    assert mt.status()["snapshot"]["adopted"] == 1


def test_a_tampered_snapshot_is_refused_and_the_last_known_good_keeps_serving(tmp_path):
    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=3))
    path = tmp_path / "agent-snapshot.json"
    bad = snapshot([m], generation=9)
    bad["models"] = {"qwen3-32b": {"Production": "666"}}  # edited after compile
    path.write_text(json.dumps(bad), encoding="utf-8")
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)

    st = mt.tick()
    assert rt.snapshot["generation"] == 3  # nothing adopted
    assert st["snapshot"]["rejected"] == 1
    assert "keeping last-known-good" in st["snapshot"]["last_error"]
    # ...and the runtime still does its job on the old configuration.
    t = rt.open_session("jobdoc", tenant="acme")
    assert rt.run_wait(t["thread_id"], "x", tenant="acme")["output"] == "ok:x"


def test_garbage_and_older_files_are_counted_not_adopted(tmp_path):
    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=5))
    path = tmp_path / "agent-snapshot.json"
    path.write_text("{not json", encoding="utf-8")
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)
    assert mt.tick()["snapshot"]["rejected"] == 1

    write_snapshot(snapshot([m], generation=4), path)
    _bump(path)
    st = mt.tick()
    assert st["snapshot"]["stale"] == 1 and rt.snapshot["generation"] == 5
    assert "older" in st["snapshot"]["last_error"]

    write_snapshot(snapshot([m], generation=6), path)  # a good file clears the error
    _bump(path)
    st = mt.tick()
    assert rt.snapshot["generation"] == 6 and st["snapshot"]["last_error"] is None


def test_a_forged_generation_cannot_lock_the_runtime_onto_one_snapshot(tmp_path):
    # Before the fix the digest left `generation` out: a file whose generation alone was edited
    # up validated, was adopted, and every genuine later snapshot was then refused as "older".
    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=3))
    path = tmp_path / "agent-snapshot.json"
    forged = snapshot([m], generation=4)
    forged["generation"] = 10**15
    path.write_text(json.dumps(forged), encoding="utf-8")
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)
    st = mt.tick()
    assert st["snapshot"]["rejected"] == 1 and rt.snapshot["generation"] == 3

    write_snapshot(snapshot([m], generation=5), path)
    _bump(path)
    mt.tick()
    assert rt.snapshot["generation"] == 5


def test_a_snapshot_with_a_non_integer_generation_is_refused_and_the_pass_never_raises(
    tmp_path,
):
    from examlops.agent_runtime.snapshot import digest_of

    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=3))
    path = tmp_path / "agent-snapshot.json"
    odd = snapshot([m], generation=4)
    odd["generation"] = None
    odd["digest"] = digest_of(odd)  # internally consistent, but not orderable
    path.write_text(json.dumps(odd), encoding="utf-8")
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)
    st = mt.tick()  # before the fix: int(None) escaped the "never raises" pass as a TypeError
    assert st["snapshot"]["rejected"] == 1 and rt.snapshot["generation"] == 3


def test_a_missing_file_is_only_an_error_while_there_is_nothing_to_serve(tmp_path):
    m = manifest()
    rt = _rt(tmp_path, snapshot([m]))
    mt = RuntimeMaintainer(rt, snapshot_path=tmp_path / "absent.json", interval=1)
    assert mt.tick()["snapshot"]["last_error"] is None

    empty = build_runtime(snapshot_path=tmp_path / "absent.json", state_db=str(tmp_path / "e.db"))
    assert empty.snapshot is None
    st = RuntimeMaintainer(empty, snapshot_path=tmp_path / "absent.json", interval=1).tick()
    assert "does not exist" in st["snapshot"]["last_error"]


# -- lifecycle sweep and crash recovery ----------------------------------------------------------


def test_a_pass_suspends_quiet_sessions(tmp_path):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])
    rt = _rt(tmp_path, snapshot([manifest()]), store=store, idle_after=60, suspend_after=300)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "a", tenant="acme")
    mt = RuntimeMaintainer(rt, interval=1)

    clock["t"] += 120
    assert mt.tick()["sweep"]["idle"] == 1
    clock["t"] += 400
    assert mt.tick()["sweep"]["suspended"] == 1
    assert rt.store.get_thread(t["thread_id"])["status"] == "suspended"


def test_a_pass_recovers_a_run_whose_worker_died(tmp_path):
    rt = _rt(tmp_path, snapshot([manifest()]))
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.submit(t["thread_id"], "late", tenant="acme")
    rt.store.update_run(run["run_id"], status="running")  # its worker died; no lease is held

    st = RuntimeMaintainer(rt, interval=1).tick()
    assert st["recovery"]["recovered"] == 1
    done = rt.store.get_run(run["run_id"])
    assert done["status"] == "success"
    assert rt.store.events(kind="run_recovered")


def test_one_failing_duty_does_not_stop_the_others(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])
    rt = _rt(tmp_path, snapshot([manifest()]), store=store, idle_after=60)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "a", tenant="acme")

    def boom(**_kw):
        raise RuntimeError("state store hiccup")

    monkeypatch.setattr(rt, "recover", boom)
    clock["t"] += 120
    st = RuntimeMaintainer(rt, interval=1).tick()  # must not raise
    assert st["recovery"]["last_error"] == "state store hiccup"
    assert st["sweep"]["idle"] == 1  # the sweep still ran


def test_the_background_loop_runs_passes_and_stops(tmp_path):
    rt = _rt(tmp_path, snapshot([manifest()]))
    mt = RuntimeMaintainer(rt, interval=0.05)
    mt.start()
    mt.start()  # idempotent
    deadline = time.time() + 5
    while mt.status()["passes"] < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert mt.status()["passes"] >= 2 and mt.running
    mt.stop()
    assert not mt.running


def test_the_interval_is_bounded(tmp_path):
    rt = _rt(tmp_path, snapshot([manifest()]))
    with pytest.raises(ValueError):
        RuntimeMaintainer(rt, interval=0)


# -- building and binding --------------------------------------------------------------------------


def test_build_runtime_falls_back_to_the_stores_last_known_good(tmp_path):
    m = manifest()
    db = str(tmp_path / "agent_state.db")
    first = build_runtime(snapshot_path=None, state_db=db)
    assert first.snapshot is None
    first.apply_snapshot(snapshot([m], generation=7))  # persisted as last-known-good

    path = tmp_path / "agent-snapshot.json"
    path.write_text("{broken", encoding="utf-8")
    again = build_runtime(snapshot_path=path, state_db=db, worker_id="w9")
    assert again.snapshot["generation"] == 7 and again.worker_id == "w9"


def test_only_installed_sandbox_providers_are_offered_and_none_means_refusal():
    from examlops.agent_runtime.sandbox import SandboxRefused, select_provider
    from examlops.agent_runtime.service import detect_sandbox_providers

    both = detect_sandbox_providers(which=lambda b: f"/usr/bin/{b}")
    assert [p.name for p in both] == ["docker", "apptainer"]
    only_apptainer = detect_sandbox_providers(which=lambda b: "/x" if b == "apptainer" else None)
    assert [p.substrate for p in only_apptainer] == ["hpc"]
    assert only_apptainer[0].capabilities().isolation == "container"
    # Nothing installed: the runtime gets no provider, and asking for isolation is refused
    # rather than running agent code unsandboxed.
    assert detect_sandbox_providers(which=lambda b: None) == []
    with pytest.raises(SandboxRefused):
        select_provider([], required="container")


def test_binding_beyond_loopback_needs_an_explicit_opt_in():
    check_bind("127.0.0.1", allow_remote=False)
    check_bind("::1", allow_remote=False)
    with pytest.raises(ValueError, match="allow-remote"):
        check_bind("0.0.0.0", allow_remote=False)
    check_bind("0.0.0.0", allow_remote=True)


def test_snapshot_path_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("EXAMLOPS_AGENT_SNAPSHOT", raising=False)
    assert snapshot_path_from_env() is None
    monkeypatch.setenv("EXAMLOPS_AGENT_SNAPSHOT", str(tmp_path / "s.json"))
    assert snapshot_path_from_env() == tmp_path / "s.json"


# -- /healthz --------------------------------------------------------------------------------------


def test_healthz_reports_maintenance_without_leaking_paths(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from examlops.agent_runtime.http import create_app

    m = manifest()
    rt = _rt(tmp_path, snapshot([m], generation=2))
    secret_dir = tmp_path / "very-private-dir"
    secret_dir.mkdir()
    path = secret_dir / "agent-snapshot.json"
    path.write_text("{broken", encoding="utf-8")
    mt = RuntimeMaintainer(rt, snapshot_path=path, interval=1)
    mt.tick()

    body = TestClient(create_app(rt, maintainer=mt)).get("/healthz").json()
    assert body["ok"] is True and body["snapshot_generation"] == 2
    assert body["maintenance"]["snapshot"]["rejected"] == 1
    assert "very-private-dir" not in json.dumps(body)


# -- the CLI ----------------------------------------------------------------------------------------


def test_cli_serve_refuses_before_listening(monkeypatch, tmp_path):
    from examlops.cli.main import app

    runner = CliRunner()
    monkeypatch.delenv("EXAMLOPS_AGENT_SNAPSHOT", raising=False)

    r = runner.invoke(app, ["--json", "agent", "runtime", "serve"])
    assert r.exit_code == 1 and json.loads(r.stdout)["code"] == "no_snapshot"

    snap = str(tmp_path / "s.json")
    r = runner.invoke(
        app, ["--json", "agent", "runtime", "serve", "--snapshot", snap, "--peer", "w2"]
    )
    assert r.exit_code == 1 and json.loads(r.stdout)["code"] == "bad_peers"

    r = runner.invoke(
        app,
        ["--json", "agent", "runtime", "serve", "--snapshot", snap, "--host", "0.0.0.0"],
    )
    assert r.exit_code == 1
    doc = json.loads(r.stdout)
    assert doc["code"] == "refused" and "allow-remote" in doc["error"]
