"""ADR 0147 decision 5 — operation handles over the control plane's real command records.

These tests run the *real* control plane app (``platform/services/control_plane/app.py``) on a real
SQLite database and route the CLI's HTTP client into it, so ``status`` / ``wait`` / ``cancel`` are
exercised against the actual ``/v1/commands`` routes, the actual ``control_plane_commands`` table
and the actual cancel semantics — no hand-built response doubles.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform/services/control_plane")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app as cp  # noqa: E402

from examlops import operations  # noqa: E402
from examlops.cli import _client  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def plane(monkeypatch, tmp_path):
    """The real control plane on a private DB; the CLI's HTTP client is routed into it."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOW_WRITES", "1")
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    monkeypatch.setattr(cp, "CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setattr(cp, "CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    # A process-wide "schema is ready" flag from another test's database would skip audit_events.
    monkeypatch.setattr(cp, "_platform_schema_ready", False)
    # No lifespan (no context manager): the worker pool must not dispatch the seeded rows.
    client = TestClient(cp.app)

    def _path(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def _check(resp):
        if resp.status_code >= 400:
            detail = resp.json().get("detail") or resp.json().get("title") or resp.text
            raise _client.ClientError(str(detail), resp.status_code)
        return resp.json()

    monkeypatch.setattr(
        _client, "get", lambda url, token="": _check(client.get(_path(url), headers=AUTH))
    )
    monkeypatch.setattr(
        _client, "delete", lambda url, token=None: _check(client.delete(_path(url), headers=AUTH))
    )
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp.test")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    return client


_clock = iter(range(10_000))


def _seed(
    key: str,
    state: str,
    *,
    run_state: str | None = None,
    flow_run: str | None = None,
    mode: str = "async",
    kind: str = "retrain",
    last_error: str | None = None,
) -> str:
    now = datetime(2026, 9, 20, 12, 0, next(_clock) % 60).isoformat()
    conn = cp._get_db()
    try:
        conn.execute(
            "INSERT INTO control_plane_commands (command_key, kind, request_hash, payload, state, "
            "response, prefect_run_id, mode, run_state, actor, tenant, last_error, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                kind,
                "h-" + key,
                json.dumps({"parameters": {"model_name": "JPCP"}}),
                state,
                json.dumps({"flow_run_id": flow_run}) if flow_run else None,
                flow_run,
                mode,
                run_state,
                "tester",
                "default",
                last_error,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return key


def _set(key: str, **cols: str) -> None:
    conn = cp._get_db()
    try:
        sets = ", ".join(f"{c}=?" for c in cols)
        conn.execute(
            f"UPDATE control_plane_commands SET {sets} WHERE command_key=?", (*cols.values(), key)
        )
        conn.commit()
    finally:
        conn.close()


def _outbox_topics() -> list[str]:
    conn = cp._get_db()
    try:
        return [r[0] for r in conn.execute("SELECT topic FROM event_outbox ORDER BY id")]
    finally:
        conn.close()


# ── normalisation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "run", "has_run", "expected"),
    [
        ("pending", None, False, "working"),
        ("dispatching", None, False, "working"),
        ("failed", None, False, "working"),  # the worker retries: not a failure yet
        ("dead", None, False, "failed"),
        ("cancelled", None, False, "cancelled"),
        ("succeeded", None, False, "completed"),
        ("succeeded", None, True, "working"),  # dispatched, run not reconciled yet
        ("succeeded", "RUNNING", True, "working"),
        ("succeeded", "COMPLETED", True, "completed"),
        ("succeeded", "FAILED", True, "failed"),
        ("succeeded", "CRASHED", True, "failed"),
        ("succeeded", "MISSING", True, "failed"),
        ("succeeded", "CANCELLED", True, "cancelled"),
        ("mystery", None, False, "unknown"),
    ],
)
def test_state_normalisation(raw, run, has_run, expected):
    assert operations.normalize(raw, run, has_run) == expected
    assert expected in (*operations.STATES, "unknown")


# ── status / list against the real control plane ─────────────────────────────


def test_status_of_a_queued_operation(plane):
    _seed("v1:retrain:q1", "pending")
    out = operations.status("v1:retrain:q1")
    assert out["ok"] is True
    op = out["operation"]
    assert (op["state"], op["raw_state"], op["terminal"], op["cancellable"]) == (
        "working",
        "pending",
        False,
        True,
    )
    assert op["operation_id"] == "v1:retrain:q1" and op["kind"] == "retrain"


def test_status_follows_the_flow_run_after_dispatch(plane):
    _seed("v1:retrain:r1", "succeeded", flow_run="flow-9", run_state="RUNNING")
    op = operations.status("v1:retrain:r1")["operation"]
    assert (op["state"], op["flow_run_id"], op["cancellable"]) == ("working", "flow-9", False)
    _set("v1:retrain:r1", run_state="COMPLETED")
    assert operations.status("v1:retrain:r1")["operation"]["state"] == "completed"


def test_a_dead_operation_is_failed_and_carries_its_error(plane):
    _seed("v1:retrain:d1", "dead", last_error="prefect unreachable")
    op = operations.status("v1:retrain:d1")["operation"]
    assert (op["state"], op["last_error"]) == ("failed", "prefect unreachable")


def test_unknown_id_and_empty_id(plane):
    missing = operations.status("v1:retrain:nope")
    assert missing["ok"] is False and missing["code"] == "not_found"
    assert operations.status("  ")["code"] == "invalid_id"


def test_list_newest_first_and_filtered_by_operation_state(plane):
    _seed("a", "pending")
    _seed("b", "dead")
    _seed("c", "succeeded", flow_run="f", run_state="COMPLETED")
    every = operations.list_operations()["operations"]
    assert [o["operation_id"] for o in every] == ["c", "b", "a"]
    working = operations.list_operations(state="working")["operations"]
    assert [o["operation_id"] for o in working] == ["a"]
    assert [
        o["operation_id"] for o in operations.list_operations(state="failed")["operations"]
    ] == ["b"]
    assert operations.list_operations(state="bogus")["code"] == "invalid_state"
    assert len(operations.list_operations(limit=2)["operations"]) == 2


# ── wait ──────────────────────────────────────────────────────────────────────


def test_wait_returns_at_once_for_a_terminal_operation(plane):
    _seed("w1", "succeeded", flow_run="f", run_state="COMPLETED")
    slept: list[float] = []
    out = operations.wait("w1", timeout=30, sleep=slept.append)
    assert out["ok"] and out["timed_out"] is False and out["operation"]["state"] == "completed"
    assert slept == []


def test_wait_polls_until_the_operation_becomes_terminal(plane):
    _seed("w2", "pending")
    polls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:  # the control plane moves the command on while we wait
            _set(
                "w2",
                state="succeeded",
                response=json.dumps({"flow_run_id": "f"}),
                prefect_run_id="f",
                run_state="COMPLETED",
            )

    now = [0.0]
    out = operations.wait(
        "w2",
        timeout=60,
        interval=1,
        sleep=fake_sleep,
        clock=lambda: now.__setitem__(0, now[0] + 1) or now[0],
    )
    assert out["timed_out"] is False and out["operation"]["state"] == "completed"
    assert len(polls) == 2


def test_wait_times_out_instead_of_blocking_forever(plane):
    _seed("w3", "pending")
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    out = operations.wait("w3", timeout=5, interval=2, sleep=sleep, clock=clock)
    assert out["ok"] is True and out["timed_out"] is True
    assert out["operation"]["state"] == "working"  # still running; a timeout is not a verdict
    assert now[0] == pytest.approx(5)  # never slept past the deadline


def test_wait_zero_looks_once_and_negative_is_refused(plane):
    _seed("w4", "pending")
    slept: list[float] = []
    out = operations.wait("w4", timeout=0, sleep=slept.append)
    assert out["timed_out"] is True and slept == []
    assert operations.wait("w4", timeout=-1)["code"] == "invalid_timeout"


def test_wait_on_an_unknown_id_returns_the_error(plane):
    assert operations.wait("ghost", timeout=0)["code"] == "not_found"


def test_wait_timeout_is_capped_and_env_configurable(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OPS_WAIT_TIMEOUT", "99999")
    assert operations.default_timeout() == operations.MAX_WAIT_TIMEOUT_S
    monkeypatch.setenv("EXAMLOPS_OPS_WAIT_TIMEOUT", "12")
    assert operations.default_timeout() == 12
    monkeypatch.setenv("EXAMLOPS_OPS_WAIT_INTERVAL", "0.5")
    assert operations.default_interval() == 0.5
    monkeypatch.setenv("EXAMLOPS_OPS_WAIT_TIMEOUT", "junk")
    assert operations.default_timeout() == operations.DEFAULT_WAIT_TIMEOUT_S


# ── cancel ────────────────────────────────────────────────────────────────────


def test_cancel_a_queued_operation_is_recorded_audited_and_published(plane):
    _seed("x1", "pending")
    out = operations.cancel("x1")
    assert out["ok"] is True and out["cancelled"] is True
    assert out["operation"]["state"] == "cancelled"
    # The record itself says so, not just our reply.
    assert operations.status("x1")["operation"]["raw_state"] == "cancelled"
    # …and the control plane audited it and published it in the same transaction.
    assert "operation.cancelled" in _outbox_topics()
    from examlops.data.audit import export_audit_events  # noqa: F401  (platform.db side)

    conn = cp._get_db()
    try:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_events")]
    finally:
        conn.close()
    assert "command_cancelled" in actions


def test_cancel_a_retrying_operation(plane):
    _seed("x2", "failed", last_error="blip")
    assert operations.cancel("x2")["cancelled"] is True


@pytest.mark.parametrize("raw", ["succeeded", "dead", "cancelled", "dispatching"])
def test_cancel_is_refused_and_honest_when_the_record_says_it_cannot(plane, raw):
    flow = "f" if raw == "succeeded" else None
    _seed("x3", raw, flow_run=flow, run_state="RUNNING" if flow else None)
    out = operations.cancel("x3")
    assert out["ok"] is False and out["code"] == "not_cancellable" and out["cancelled"] is False
    assert operations.status("x3")["operation"]["raw_state"] == raw  # untouched
    assert "operation.cancelled" not in _outbox_topics()


def test_cancel_never_claims_success_for_an_already_terminal_operation(plane):
    _seed("x4", "succeeded", flow_run="f", run_state="COMPLETED")
    out = operations.cancel("x4")
    assert out["cancelled"] is False and "already completed" in out["error"]


def test_a_sync_mode_command_cannot_be_cancelled_and_the_409_is_honest(plane):
    _seed("x5", "pending", mode="sync")
    out = operations.cancel("x5")  # the record looks cancellable; the control plane refuses
    assert out["ok"] is False and out["code"] == "not_cancellable" and out["cancelled"] is False
    assert operations.status("x5")["operation"]["raw_state"] == "pending"


def test_cancel_unknown_id(plane):
    out = operations.cancel("ghost")
    assert out["ok"] is False and out["code"] == "not_found"


def test_an_unreachable_control_plane_is_a_result_not_a_crash(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(_client, "get", boom)
    out = operations.status("anything", base="http://x", token="t")
    assert out["ok"] is False and out["code"] == "control_plane_unreachable"


# ── CLI ───────────────────────────────────────────────────────────────────────


def _exa(*args: str):
    from examlops.cli.main import app

    return CliRunner().invoke(app, list(args))


def test_cli_status_json_and_exit_codes(plane):
    _seed("v1:retrain:c1", "pending")
    ok = _exa("--json", "ops", "status", "v1:retrain:c1")
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["state"] == "working"
    missing = _exa("--json", "ops", "status", "ghost")
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["hint"] == "not_found"


def test_cli_list_json(plane):
    _seed("l1", "pending")
    _seed("l2", "dead")
    res = _exa("--json", "ops", "list", "--state", "failed")
    assert res.exit_code == 0, res.output
    assert [o["operation_id"] for o in json.loads(res.stdout)] == ["l2"]


def test_cli_wait_exit_codes(plane):
    _seed("wc", "succeeded", flow_run="f", run_state="COMPLETED")
    _seed("wf", "dead", last_error="x")
    _seed("wq", "pending")
    done = _exa("--json", "ops", "wait", "wc", "--timeout", "0")
    assert done.exit_code == 0 and json.loads(done.stdout)["timed_out"] is False
    failed = _exa("--json", "ops", "wait", "wf", "--timeout", "0")
    assert failed.exit_code == 1 and json.loads(failed.stdout)["state"] == "failed"
    slow = _exa("--json", "ops", "wait", "wq", "--timeout", "0")
    assert slow.exit_code == 124
    body = json.loads(slow.stdout)
    assert body["timed_out"] is True and body["state"] == "working"


def test_cli_cancel_ok_refused_and_audited(plane):
    _seed("cc", "pending")
    _seed("cd", "succeeded", flow_run="f", run_state="COMPLETED")
    res = _exa("--json", "ops", "cancel", "cc")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["cancelled"] is True
    refused = _exa("--json", "ops", "cancel", "cd")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["code"] == "not_cancellable"
    from examlops.data.audit import export_audit_events

    events = [e for e in export_audit_events() if e["action"] == "operation_cancel_requested"]
    assert {e["target"] for e in events} == {"cc", "cd"}  # the refused request is on record too


def test_cli_cancel_refuses_an_agent_principal_without_a_plan(plane, monkeypatch):
    _seed("ca", "pending")
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    res = _exa("--json", "ops", "cancel", "ca")
    assert res.exit_code != 0
    assert operations.status("ca")["operation"]["raw_state"] == "pending"


# ── MCP ───────────────────────────────────────────────────────────────────────


def test_mcp_operation_status_is_a_read_tool_and_reads_the_record(plane):
    from examlops.mcp import tools

    spec = next(s for s in tools.REGISTRY if s.name == "operation_status")
    assert spec.mutating is False and spec.annotations["readOnlyHint"] is True
    _seed("m1", "pending")
    out = tools.operation_status("m1")
    assert out["ok"] and out["operation"]["state"] == "working"
    assert tools.operation_status("ghost")["code"] == "not_found"


def test_mcp_operation_cancel_is_mutating_idempotent_and_audited(plane):
    from examlops.mcp import tools

    spec = next(s for s in tools.REGISTRY if s.name == "operation_cancel")
    assert spec.mutating is True and spec.tier == "A"
    assert (
        spec.annotations["destructiveHint"] is True and spec.annotations["idempotentHint"] is True
    )
    _seed("m2", "pending")
    cancel = spec.fn
    out = cancel("m2", idempotency_key="k-m2")
    assert out["ok"] and out["cancelled"] is True and "audit_warning" not in out
    replay = cancel("m2", idempotency_key="k-m2")
    assert replay["replayed"] is True and replay["cancelled"] is True
    events = [
        e
        for e in tools.recent_audit_events(limit=50)["events"]
        if e["action"] == "operation_cancel_requested"
    ]
    assert len(events) == 1  # the replay produced no second audit event


def test_mcp_operation_cancel_refusal_is_not_a_cancel_and_is_audited(plane):
    from examlops.mcp import tools

    _seed("m3", "succeeded", flow_run="f", run_state="COMPLETED")
    out = tools.operation_cancel("m3")
    assert out["ok"] is False and out["cancelled"] is False and out["code"] == "not_cancellable"
    actions = [e["action"] for e in tools.recent_audit_events(limit=50)["events"]]
    assert "operation_cancel_requested" in actions


def test_mcp_operation_cancel_audit_failure_warns_but_reports_the_cancel(plane):
    from unittest.mock import patch

    from examlops.mcp import tools

    _seed("m4", "pending")

    def boom(*_a, **_k):
        raise RuntimeError("audit chain unavailable")

    with patch("examlops.data.audit.write_audit_event", boom):
        out = tools.operation_cancel("m4")
    assert out["ok"] is True and out["cancelled"] is True and "not audited" in out["audit_warning"]


def test_mcp_agent_principal_needs_a_plan_to_cancel(plane, monkeypatch):
    from examlops.mcp import tools

    _seed("m5", "pending")
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    direct = tools.REGISTRY[[s.name for s in tools.REGISTRY].index("operation_cancel")].fn("m5")
    assert direct["ok"] is False and direct["code"] == "plan_required"
    plan = tools.plan_change("operation_cancel", {"operation_id": "m5"})
    assert plan["ok"], plan
    assert (
        operations.status("m5")["operation"]["raw_state"] == "pending"
    )  # planning changed nothing
    applied = tools.apply_plan(plan["plan"]["plan_hash"])
    assert applied["ok"], applied
    assert operations.status("m5")["operation"]["raw_state"] == "cancelled"


def test_mcp_apply_fails_when_the_operation_moved_on_after_planning(plane, monkeypatch):
    from examlops.mcp import tools

    _seed("m6", "pending")
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    plan = tools.plan_change("operation_cancel", {"operation_id": "m6"})["plan"]
    _set("m6", state="dispatching")  # the world changed
    applied = tools.apply_plan(plan["plan_hash"])
    assert applied["ok"] is False
    assert operations.status("m6")["operation"]["raw_state"] == "dispatching"


# ── the handle is what the starting call returns ─────────────────────────────


def test_a_retrain_submission_returns_the_operation_handle(plane):
    from examlops import retrain_command

    view = {
        "command_id": "v1:retrain:h1",
        "state": "succeeded",
        "status_url": "/v1/commands/v1:retrain:h1",
        "result": {"flow_run_id": "flow-1"},
    }
    ans = retrain_command.outcome(view)
    assert ans["operation_id"] == ans["command_id"] == "v1:retrain:h1"
    pending = retrain_command.outcome({**view, "state": "pending", "result": None})
    assert pending["operation_id"] == "v1:retrain:h1"


def test_the_control_plane_publishes_operation_cancelled_under_its_schema(plane):
    from examlops.events import schemas

    assert "operation.cancelled" in schemas.SCHEMAS
    _seed("ev1", "pending")
    operations.cancel("ev1")
    conn = cp._get_db()
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM event_outbox WHERE topic='operation.cancelled'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert schemas.validate("operation.cancelled", payload) == []
    assert payload["command_key"] == "ev1"


def test_a_cancel_request_that_could_not_be_audited_is_counted(plane, monkeypatch):
    """The CLI's audit is best-effort; a lost one must show in the drop counters, not vanish."""
    from examlops.data import audit
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    reset_dropped_audit_events()

    def boom(*_a, **_k):
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr(audit, "write_audit_event", boom)
    _seed("ad1", "pending")
    res = _exa("--json", "ops", "cancel", "ad1")
    assert res.exit_code == 0, res.output  # the cancel stands
    assert "operation_cancel_requested" in dropped_audit_events(), dropped_audit_events()
    reset_dropped_audit_events()
