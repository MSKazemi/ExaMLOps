"""CLI Console (ADR 0119) — the dashboard runs every `exa` command through the platform's surface.

These run the **real** CLI in a subprocess against a per-test `platform.db`: the claim under test
is "the dashboard can do what the CLI does", and a mocked CLI would prove nothing about that.
Commands used here are pure-datastore ones (`project …`), so no backing service is contacted.
"""

from __future__ import annotations

import asyncio
import sys

import cli_runner
import dbconn
import pytest
from routers import cli as cli_router

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.setenv("EXAMLOPS_DASHBOARD_CLI_WORKSPACE", str(tmp_path / "ws"))
    # The CLI's own config (contexts, active project) must not leak in from the developer's home.
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    from examlops import data as pdb

    pdb.init_db()
    return str(db)


@pytest.fixture(autouse=True)
def fresh_runner(monkeypatch):
    """A private runner per test, backed by the shared store as in production (it resolves the
    per-test PLATFORM_DB at call time), so run tables never leak between tests."""
    import cli_store

    runner = cli_runner.CliRunner(
        max_concurrent=4, per_user=2, timeout=120, store=cli_store.CliRunStore()
    )
    monkeypatch.setattr(cli_runner, "RUNNER", runner)
    return runner


async def _token(client, pw):
    r = await client.post("/api/auth/login", json={"password": pw})
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _run(client, headers, command, args=None, **extra):
    body = {"command": command, "args": args or {}, **extra}
    return await client.post("/api/v1/cli/runs", json=body, headers=headers)


async def _finish(client, headers, run_id, runner):
    await runner.wait(run_id, timeout=120)
    r = await client.get(f"/api/v1/cli/runs/{run_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _audit_rows(db, action):
    conn = dbconn.connect(db, row_factory=None)
    try:
        return conn.execute(
            "SELECT actor, target, details FROM audit_events WHERE action=? ORDER BY id", (action,)
        ).fetchall()
    finally:
        conn.close()


# ── catalog ───────────────────────────────────────────────────────────────────────────────


async def test_catalog_lists_every_cli_command(client, platform_db):
    from examlops.cli import surface

    h = await _token(client, VIEWER_PW)
    r = await client.get("/api/v1/cli/catalog", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(surface.leaf_paths())
    assert body["unclassified"] == []
    paths = {c["path"] for c in body["commands"]}
    assert {"drift status", "backup create", "stack down", "exchange pack"} <= paths


async def test_catalog_is_gzipped_when_the_browser_accepts_it(client, platform_db):
    h = await _token(client, VIEWER_PW)
    r = await client.get("/api/v1/cli/catalog", headers={**h, "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert r.json()["total"] > 300  # httpx transparently inflated it


async def test_catalog_needs_a_login(client, platform_db):
    r = await client.get("/api/v1/cli/catalog")
    assert r.status_code == 401


# ── authorization by tier ─────────────────────────────────────────────────────────────────


async def test_viewer_runs_a_read_command_end_to_end(client, platform_db, fresh_runner):
    h = await _token(client, VIEWER_PW)
    r = await _run(client, h, "project list")
    assert r.status_code == 202, r.text
    run = await _finish(client, h, r.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run
    assert run["exit_code"] == 0
    assert run["parsed"] == []
    assert run["display"] == "exa --json project list"


async def test_viewer_cannot_run_a_write(client, platform_db):
    h = await _token(client, VIEWER_PW)
    r = await _run(client, h, "project create", {"name": "nope"})
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()


async def test_arguments_that_persist_escalate_a_read_to_admin(client, platform_db, fresh_runner):
    # `hpc gpu-share plan` is a pure calculation; `--record` makes it write the allocation.
    h = await _token(client, VIEWER_PW)
    ok = await _run(client, h, "hpc gpu-share plan", {"model": "JPCP"})
    assert ok.status_code == 202, ok.text
    run = await _finish(client, h, ok.json()["id"], fresh_runner)
    assert run["parsed"]["mechanism"] == "whole", run
    denied = await _run(client, h, "hpc gpu-share plan", {"model": "JPCP", "record": True})
    assert denied.status_code == 403
    assert "changes platform state" in denied.json()["detail"]


async def test_admin_write_runs_the_real_cli_and_is_audited(client, platform_db, fresh_runner):
    h = await _token(client, ADMIN_PW)
    r = await _run(client, h, "project create", {"name": "console-p1", "description": "via UI"})
    assert r.status_code == 202, r.text
    run = await _finish(client, h, r.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run

    shown = await _run(client, h, "project show", {"name": "console-p1"})
    detail = await _finish(client, h, shown.json()["id"], fresh_runner)
    assert detail["parsed"]["description"] == "via UI"

    started = _audit_rows(platform_db, "cli_run")
    finished = _audit_rows(platform_db, "cli_run_finished")
    assert any(t == "exa project create" and a.startswith("dashboard:admin") for a, t, _ in started)
    assert any(t == "exa project create" and '"succeeded"' in d for _, t, d in finished)


async def test_the_cli_records_the_dashboard_user_as_the_actor(client, platform_db, fresh_runner):
    h = await _token(client, ADMIN_PW)
    r = await _run(client, h, "project create", {"name": "actor-p"})
    await _finish(client, h, r.json()["id"], fresh_runner)
    conn = dbconn.connect(platform_db, row_factory=None)
    try:
        created_by = conn.execute(
            "SELECT created_by FROM projects WHERE name='actor-p'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert created_by.startswith("dashboard:admin")


async def test_destructive_needs_the_command_typed_back(client, platform_db, fresh_runner):
    h = await _token(client, ADMIN_PW)
    made = await _run(client, h, "project create", {"name": "doomed"})
    await _finish(client, h, made.json()["id"], fresh_runner)

    r = await _run(client, h, "project delete", {"name": "doomed"})
    assert r.status_code == 409
    wrong = await _run(client, h, "project delete", {"name": "doomed"}, confirm="project")
    assert wrong.status_code == 409
    ok = await _run(client, h, "project delete", {"name": "doomed"}, confirm="project delete")
    assert ok.status_code == 202, ok.text
    run = await _finish(client, h, ok.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run


async def test_cli_only_commands_are_refused_with_their_reason(client, platform_db):
    h = await _token(client, ADMIN_PW)
    r = await _run(client, h, "stack down")
    assert r.status_code == 400
    assert "Services console" in r.json()["detail"]


async def test_unknown_command_and_unknown_parameter(client, platform_db):
    h = await _token(client, ADMIN_PW)
    assert (await _run(client, h, "project frobnicate")).status_code == 404
    r = await _run(client, h, "project list", {"shell": "id"})
    assert r.status_code == 400


async def test_blocked_flags_are_refused(client, platform_db):
    h = await _token(client, ADMIN_PW)
    r = await _run(client, h, "status", {"watch": True})
    assert r.status_code == 400


async def test_paths_outside_the_workspace_are_refused(client, platform_db):
    h = await _token(client, ADMIN_PW)
    for bad in ("/etc/passwd", "../../x", "~/x"):
        r = await _run(client, h, "docs", {"out": bad})
        assert r.status_code == 400, bad


async def test_bad_format_and_context_are_refused(client, platform_db):
    h = await _token(client, VIEWER_PW)
    assert (await _run(client, h, "project list", format="xml")).status_code == 400
    assert (await _run(client, h, "project list", context="a;b")).status_code == 400


async def test_concurrent_runs_keep_the_audit_hash_chain_intact(client, platform_db, monkeypatch):
    # Regression: the router audited on a fresh, idle connection, so concurrent runs each read
    # the same chain head and appended — five `cli_run` rows with one `prev_hash`, a forked chain
    # that `exa audit verify` reports as tampered. Audit writes must take the chain's own lock.
    import asyncio

    from examlops.data.audit import verify_audit_chain

    runner = cli_runner.CliRunner(max_concurrent=8, per_user=8, timeout=120)
    monkeypatch.setattr(cli_runner, "RUNNER", runner)
    h = await _token(client, ADMIN_PW)
    started = await asyncio.gather(
        *[_run(client, h, "project create", {"name": f"race-{i}"}) for i in range(8)]
    )
    assert all(r.status_code == 202 for r in started), [r.text for r in started]
    for r in started:
        await runner.wait(r.json()["id"], timeout=120)
    result = verify_audit_chain()
    assert result["ok"], result


# ── visibility + cancellation ─────────────────────────────────────────────────────────────


async def test_a_viewer_sees_only_their_own_runs(client, platform_db, fresh_runner):
    admin = await _token(client, ADMIN_PW)
    r = await _run(client, admin, "project list")
    run_id = r.json()["id"]
    await fresh_runner.wait(run_id)

    viewer = await _token(client, VIEWER_PW)
    assert (await client.get(f"/api/v1/cli/runs/{run_id}", headers=viewer)).status_code == 404
    listed = (await client.get("/api/v1/cli/runs", headers=viewer)).json()["runs"]
    assert run_id not in {x["id"] for x in listed}
    everyone = (await client.get("/api/v1/cli/runs", headers=admin)).json()["runs"]
    assert run_id in {x["id"] for x in everyone}


async def test_the_runner_refuses_rather_than_queueing_without_limit(
    client, platform_db, monkeypatch
):
    monkeypatch.setattr(cli_runner, "RUNNER", cli_runner.CliRunner(max_concurrent=0))
    h = await _token(client, VIEWER_PW)
    r = await _run(client, h, "project list")
    assert r.status_code == 429


# ── workspace ─────────────────────────────────────────────────────────────────────────────


async def test_workspace_round_trip_feeds_a_command_and_collects_its_output(
    client, platform_db, fresh_runner
):
    h = await _token(client, ADMIN_PW)
    up = await client.post(
        "/api/v1/cli/workspace",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"path": "inputs/notes.txt"},
        headers=h,
    )
    assert up.status_code == 201, up.text
    dup = await client.post(
        "/api/v1/cli/workspace",
        files={"file": ("notes.txt", b"again", "text/plain")},
        data={"path": "inputs/notes.txt"},
        headers=h,
    )
    assert dup.status_code == 409

    r = await _run(client, h, "docs", {"out": "out/cli.md"}, format="text")
    run = await _finish(client, h, r.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run
    assert "out/cli.md" in run["files"]

    listed = (await client.get("/api/v1/cli/workspace", headers=h)).json()["files"]
    assert {"inputs/notes.txt", "out/cli.md"} <= {f["path"] for f in listed}
    got = await client.get("/api/v1/cli/workspace/file", params={"path": "out/cli.md"}, headers=h)
    assert got.status_code == 200 and b"exa" in got.content

    gone = await client.delete(
        "/api/v1/cli/workspace/file", params={"path": "inputs/notes.txt"}, headers=h
    )
    assert gone.status_code == 200
    assert _audit_rows(platform_db, "cli_workspace_upload")
    assert _audit_rows(platform_db, "cli_workspace_delete")


async def test_an_output_path_in_a_new_directory_just_works(client, platform_db, fresh_runner):
    # In a terminal you would `mkdir exports` first; the console has no mkdir, so it makes the
    # parents of every (already-contained) path before the run. `audit export` does not.
    h = await _token(client, ADMIN_PW)
    r = await _run(client, h, "audit export", {"out": "exports/2026/audit.json"})
    assert r.status_code == 202, r.text
    run = await _finish(client, h, r.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run
    assert "exports/2026/audit.json" in run["files"]


async def test_commands_run_from_the_repo_root_like_an_operator(client, platform_db, fresh_runner):
    # `exa seanerbus list` reads the use-case pack relative to the repo root ("run from the repo
    # root"); from the workspace it failed. The console runs where an operator would.
    h = await _token(client, VIEWER_PW)
    r = await _run(client, h, "seanerbus list")
    run = await _finish(client, h, r.json()["id"], fresh_runner)
    assert run["status"] == "succeeded", run


def test_the_default_workspace_is_outside_the_repo(monkeypatch, tmp_path):
    # Next to PLATFORM_DB meant the repo root in local dev (PLATFORM_DB=./platform.db): an
    # untracked cli-workspace/ in the working tree. The default is the user's XDG state dir.
    monkeypatch.delenv("EXAMLOPS_DASHBOARD_CLI_WORKSPACE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert (
        cli_runner.workspace_root() == (tmp_path / "state" / "examlops" / "cli-workspace").resolve()
    )
    monkeypatch.setenv("EXAMLOPS_DASHBOARD_CLI_WORKSPACE", str(tmp_path / "explicit"))
    assert cli_runner.workspace_root() == (tmp_path / "explicit").resolve()


def test_run_directory_prefers_explicit_then_repo_root(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_DASHBOARD_CLI_CWD", str(tmp_path))
    assert cli_runner.run_cwd(tmp_path / "ws") == tmp_path.resolve()
    monkeypatch.delenv("EXAMLOPS_DASHBOARD_CLI_CWD")
    monkeypatch.setenv("REPO_ROOT", str(tmp_path / "missing"))
    detected = cli_runner.run_cwd(tmp_path / "ws")
    assert (detected / "pipelines").is_dir(), "falls back to the repo examlops is loaded from"


async def test_workspace_is_admin_only_and_contained(client, platform_db):
    viewer = await _token(client, VIEWER_PW)
    assert (await client.get("/api/v1/cli/workspace", headers=viewer)).status_code == 403
    admin = await _token(client, ADMIN_PW)
    r = await client.get("/api/v1/cli/workspace/file", params={"path": "../x"}, headers=admin)
    assert r.status_code == 400
    up = await client.post(
        "/api/v1/cli/workspace",
        files={"file": ("x", b"x", "text/plain")},
        data={"path": "/etc/cron.d/x"},
        headers=admin,
    )
    assert up.status_code == 400


# ── the runner itself ─────────────────────────────────────────────────────────────────────


def test_child_env_drops_the_dashboards_credentials(monkeypatch):
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "jwt")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "kept-for-the-secrets-store")
    env = cli_runner.child_env("dashboard:admin@x")
    assert "DASHBOARD_JWT_SECRET" not in env
    assert "DASHBOARD_ADMIN_PASSWORD" not in env
    assert "DATABASE_URL" not in env
    assert env["DASHBOARD_SECRET_KEY"] == "kept-for-the-secrets-store"
    assert env["EXAMLOPS_ACTOR"] == "dashboard:admin@x"
    assert env["NO_COLOR"] == "1"


def test_display_command_is_pasteable_and_masks_secrets():
    shown = cli_runner.display_command(
        ["secrets", "set", "--", "cp/token", "***"], "json", "staging"
    )
    assert shown == "exa --json --context staging secrets set cp/token '***'"
    dashed = cli_runner.display_command(["drift", "status", "--", "-x"], "text", "")
    assert dashed == "exa drift status -- -x"


async def test_a_run_past_its_timeout_is_stopped(tmp_path):
    slow = cli_runner.CliRunner(timeout=0.5, python=str(_sleeper(tmp_path)))
    run = slow.submit(
        command="sleep",
        argv=[],
        display="sleep",
        tier="read",
        fmt="json",
        context="",
        actor="t",
        role="viewer",
        args={},
        workspace=tmp_path,
    )
    done = await slow.wait(run.id, timeout=30)
    assert done.status == "timeout"
    assert "stopped after" in done.error


async def test_cancel_stops_a_running_command(tmp_path):
    slow = cli_runner.CliRunner(timeout=60, python=str(_sleeper(tmp_path)))
    run = slow.submit(
        command="sleep",
        argv=[],
        display="sleep",
        tier="read",
        fmt="json",
        context="",
        actor="t",
        role="viewer",
        args={},
        workspace=tmp_path,
    )
    await asyncio.sleep(0.3)
    assert await slow.cancel(run.id) is True
    done = await slow.wait(run.id, timeout=30)
    assert done.status == "cancelled"
    assert await slow.cancel(run.id) is False  # already terminal


def _marker_script(tmp_path):
    """A 'command' whose only effect is a file — present iff the command actually ran."""
    marker = tmp_path / "ran"
    script = tmp_path / "effect.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexec sleep 30\n")
    script.chmod(0o755)
    return str(script), marker


def _submit_effect(runner, tmp_path):
    return runner.submit(
        command="effect",
        argv=[],
        display="effect",
        tier="admin",
        fmt="json",
        context="",
        actor="t",
        role="admin",
        args={},
        workspace=tmp_path,
    )


async def test_a_run_cancelled_before_it_starts_never_runs(tmp_path):
    # Cancel lands while the run is still queued: "cancelled" must mean the command did not run,
    # not that it ran to completion under a cancelled label (seen: spawned anyway, to the timeout).
    script, marker = _marker_script(tmp_path)
    runner = cli_runner.CliRunner(timeout=60, python=script)
    run = _submit_effect(runner, tmp_path)
    assert await runner.cancel(run.id) is True  # no await in between: not yet spawned
    done = await runner.wait(run.id, timeout=10)
    assert done.status == "cancelled"
    assert done.finished_at is not None
    assert not marker.exists()
    assert run.id not in runner._procs


async def test_a_cancel_flag_raised_before_spawn_is_honoured_too(tmp_path, monkeypatch):
    # The cross-replica path: another replica raised the flag while this one was about to spawn.
    import cli_store

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    script, marker = _marker_script(tmp_path)
    store = cli_store.CliRunStore()
    runner = cli_runner.CliRunner(timeout=60, python=script, store=store)
    run = _submit_effect(runner, tmp_path)
    assert store.request_cancel(run.id) is True  # as another replica would, before any await
    done = await runner.wait(run.id, timeout=10)
    assert done.status == "cancelled"
    assert not marker.exists()
    assert store.get(run.id)["status"] == "cancelled"


async def test_output_is_capped_but_the_pipe_keeps_draining(tmp_path):
    loud = cli_runner.CliRunner(max_output=1000, python=str(_shouter(tmp_path)))
    run = loud.submit(
        command="shout",
        argv=[],
        display="shout",
        tier="read",
        fmt="json",
        context="",
        actor="t",
        role="viewer",
        args={},
        workspace=tmp_path,
    )
    done = await loud.wait(run.id, timeout=30)
    assert done.status == "succeeded"  # did not deadlock on a full pipe
    assert done.truncated is True
    assert len(done.stdout) == 1000


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _sleeper(tmp_path):
    # Stands in for the interpreter: ignores `-m examlops.cli …` and just sleeps.
    return _script(tmp_path, "sleeper.sh", "exec sleep 30")


def _shouter(tmp_path):
    return _script(tmp_path, "shouter.sh", f"{sys.executable} -c \"print('x' * 500_000)\"")


def test_router_formats_map_text_to_the_cli_table_output():
    assert cli_router._FORMATS == {"json": "json", "text": "table"}


# ── kill switch (F25 flag ``cliConsole``) ─────────────────────────────────────────────────


def _switch(db, enabled):
    import feature_flags

    assert feature_flags.set_override(db, cli_router.FLAG, enabled, "admin") is True


async def test_switching_the_console_off_closes_every_endpoint_server_side(client, platform_db):
    _switch(platform_db, False)
    h = await _token(client, ADMIN_PW)
    calls = [
        client.get("/api/v1/cli/catalog", headers=h),
        _run(client, h, "project list"),
        client.get("/api/v1/cli/runs", headers=h),
        client.get("/api/v1/cli/runs/any", headers=h),
        client.get("/api/v1/cli/workspace", headers=h),
        client.get("/api/v1/cli/workspace/file", params={"path": "a.txt"}, headers=h),
        client.delete("/api/v1/cli/workspace/file", params={"path": "a.txt"}, headers=h),
        client.post("/api/v1/cli/workspace", files={"file": ("a.txt", b"x")}, headers=h),
    ]
    for response in await asyncio.gather(*calls):
        assert response.status_code == 403, (response.request.url, response.text)
        assert "cliConsole" in response.json()["detail"]


async def test_the_switch_is_live_no_restart_needed(client, platform_db):
    h = await _token(client, VIEWER_PW)
    assert (await client.get("/api/v1/cli/runs", headers=h)).status_code == 200
    _switch(platform_db, False)
    assert (await client.get("/api/v1/cli/runs", headers=h)).status_code == 403
    _switch(platform_db, True)
    assert (await client.get("/api/v1/cli/runs", headers=h)).status_code == 200


async def test_a_run_already_going_can_still_be_cancelled_after_the_switch_is_off(
    client, platform_db, fresh_runner, tmp_path
):
    sleeper = tmp_path / "sleep.sh"
    sleeper.write_text("#!/bin/sh\nexec sleep 30\n")
    sleeper.chmod(0o755)
    fresh_runner.python = str(sleeper)
    h = await _token(client, ADMIN_PW)
    started = await _run(client, h, "project list")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    _switch(platform_db, False)
    r = await client.post(f"/api/v1/cli/runs/{run_id}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert (await fresh_runner.wait(run_id, timeout=20)).status == "cancelled"


async def test_the_flag_is_reported_to_the_browser(client, platform_db):
    h = await _token(client, VIEWER_PW)
    assert (await client.get("/api/v1/flags", headers=h)).json()["flags"]["cliConsole"] is True
    _switch(platform_db, False)
    assert (await client.get("/api/v1/flags", headers=h)).json()["flags"]["cliConsole"] is False


# ── live output (a long run shows what it has printed so far) ─────────────────────────────


def _talker(tmp_path):
    # Prints a line straight away, then keeps running: the line must be visible before the end.
    return _script(
        tmp_path, "talker.sh", "echo 'step 1 of 3: fetching'\necho 'a warning' >&2\nexec sleep 30"
    )


async def _until(predicate, timeout=8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.1)
    return predicate()


async def test_a_running_command_shows_its_output_so_far(tmp_path):
    runner = cli_runner.CliRunner(timeout=60, python=_talker(tmp_path))
    run = runner.submit(
        command="talk",
        argv=[],
        display="talk",
        tier="read",
        fmt="table",
        context="",
        actor="t",
        role="viewer",
        args={},
        workspace=tmp_path,
    )
    try:
        seen = await _until(lambda: "step 1 of 3" in runner.get(run.id).stdout)
        assert seen, "output printed so far was not visible while the command ran"
        live = runner.get(run.id)
        assert live.status == "running"
        assert "a warning" in live.stderr
        assert live.detail()["stdout"].startswith("step 1 of 3")
    finally:
        await runner.cancel(run.id)
        await runner.wait(run.id, timeout=20)


async def test_the_api_serves_a_running_commands_output_so_far(
    client, platform_db, fresh_runner, tmp_path
):
    fresh_runner.python = _talker(tmp_path)
    h = await _token(client, ADMIN_PW)
    started = await _run(client, h, "project list", format="text")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    try:
        body = {}
        for _ in range(80):
            body = (await client.get(f"/api/v1/cli/runs/{run_id}", headers=h)).json()
            if "step 1 of 3" in body.get("stdout", ""):
                break
            await asyncio.sleep(0.1)
        assert body["status"] == "running", body
        assert "step 1 of 3" in body["stdout"]
        listed = (await client.get("/api/v1/cli/runs", headers=h)).json()
        assert any(r["id"] == run_id for r in listed["runs"])
    finally:
        await client.post(f"/api/v1/cli/runs/{run_id}/cancel", headers=h)
        await fresh_runner.wait(run_id, timeout=20)


async def test_a_truncated_workspace_listing_says_so(client, platform_db, fresh_runner):
    """The listing stops at 2000 files and used to say nothing about the rest.

    A workspace with more files than that returned a full-looking list, so an operator looking for
    the output a command had just written could conclude it was never produced. The cap is right —
    the endpoint must not stream an unbounded tree — but a truncated answer has to admit it is one.
    """
    import cli_runner

    root = cli_runner.workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    for i in range(2100):
        (root / f"f{i:05d}.txt").write_bytes(b"x")

    h = await _token(client, ADMIN_PW)
    body = (await client.get("/api/v1/cli/workspace", headers=h)).json()
    assert len(body["files"]) == 2000
    assert body.get("truncated") is True, f"a truncated listing did not say so: {body.keys()}"


async def test_a_complete_workspace_listing_is_not_marked_truncated(
    client, platform_db, fresh_runner
):
    """Anti-vacuity: the flag must follow the listing, not always be set."""
    import cli_runner

    root = cli_runner.workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "one.txt").write_bytes(b"x")

    h = await _token(client, ADMIN_PW)
    body = (await client.get("/api/v1/cli/workspace", headers=h)).json()
    assert body.get("truncated") is False
