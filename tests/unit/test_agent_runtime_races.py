"""Adversarial-review regressions for the agent runtime (ADR 0144 d4/d7, ADR 0146 d2/d4).

Each test reproduces a failure the first build had and asserts the outcome - quota held, a
closed session staying closed, a rollback policy applied once, a pin that is not lost - not the
calls made. Races are made deterministic by serving the runtime the stale read a concurrent
request would have seen.
"""

from __future__ import annotations

import pytest

from examlops.agent_runtime import AgentStateStore, BrokerGateway, Node, RuntimeRefusal
from examlops.agent_versions.manifest import version_id_of
from tests.unit._agent_runtime_fixtures import (
    Tools,
    linear_program,
    make_runtime,
    manifest,
    snapshot,
)


@pytest.fixture(autouse=True)
def _no_policy(monkeypatch):
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])


def _echo():
    return linear_program(Node("echo", lambda state, ctx: {"output": state["input"]}))


def _rt(tmp_path, snap, *, store=None, programs=None, **kw):
    tools = Tools()
    rt = make_runtime(
        tmp_path,
        snap,
        programs or {"tests.jobdoc:program": _echo()},
        store=store or AgentStateStore(str(tmp_path / "agent_state.db")),
        **kw,
    )
    rt.gateway = BrokerGateway(tools=tools.specs, grant_resolver=rt._grants_for)
    return rt, tools


def _quota(n):
    return {"default": {"max_sessions": n}, "tenants": {}}


# -- the session quota is checked where the slot is taken ---------------------------------------


def test_two_concurrent_opens_cannot_share_the_last_quota_slot(tmp_path, monkeypatch):
    """Both requests passed the early count before either inserted: the insert must refuse."""
    m = manifest()
    rt, _ = _rt(tmp_path, snapshot([m], quotas=_quota(1)))
    rt.open_session("jobdoc", tenant="acme")
    # The second request's early count ran before the first request's insert committed.
    monkeypatch.setattr(rt.store, "count_threads", lambda **kw: 0)
    with pytest.raises(RuntimeRefusal) as exc:
        rt.open_session("jobdoc", tenant="acme")
    assert exc.value.code == "session_quota_exceeded" and exc.value.status == 429
    held = rt.store.list_threads(tenant="acme", status="active")
    assert len(held) == 1  # the quota held: no second session was created


def test_answering_an_interrupt_on_a_suspended_session_is_held_to_the_quota(tmp_path):
    """A session parked on a human is swept to `suspended`; answering it is a resume."""
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])

    def ask(state, ctx):
        return {"output": ctx.interrupt({"q": "go?"})}

    m = manifest()
    rt, _ = _rt(
        tmp_path,
        snapshot([m], quotas=_quota(1)),
        store=store,
        programs={"tests.jobdoc:program": linear_program(Node("ask", ask))},
        idle_after=60,
        suspend_after=300,
    )
    parked = rt.open_session("jobdoc", tenant="acme")
    assert rt.run_wait(parked["thread_id"], "x", tenant="acme")["status"] == "interrupted"
    clock["t"] += 400
    assert rt.sweep()["suspended"] == [parked["thread_id"]]
    rt.open_session("jobdoc", tenant="acme")  # the freed slot is taken by another session

    with pytest.raises(RuntimeRefusal) as exc:
        rt.resume(parked["thread_id"], "yes", tenant="acme", by="alice")
    assert exc.value.code == "session_quota_exceeded"
    assert rt.usage("acme")["active_sessions"] == 1  # still within the quota
    # ...and the answer was not consumed: it can be given once a slot is free.
    assert rt.store.get_thread(parked["thread_id"])["status"] == "suspended"
    run = rt.store.runs_for_thread(parked["thread_id"])[-1]
    assert rt.store.open_interrupt(run["run_id"]) is not None


# -- the lifecycle is compare-and-set -----------------------------------------------------------


def test_the_sweep_does_not_reopen_a_session_closed_while_it_ran(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])
    m = manifest()
    rt, _ = _rt(tmp_path, snapshot([m]), store=store, idle_after=60, suspend_after=300)
    t = rt.open_session("jobdoc", tenant="acme")
    clock["t"] += 400
    stale_page = rt.store.list_threads(tenant=None, status="active")  # the sweep's read...
    rt.close_session(t["thread_id"], tenant="acme")  # ...then the user closes the session

    real = rt.store.list_threads
    monkeypatch.setattr(
        rt.store,
        "list_threads",
        lambda **kw: stale_page if kw.get("status") == "active" else real(**kw),
    )
    moved = rt.sweep()
    assert moved["suspended"] == []
    assert rt.store.get_thread(t["thread_id"])["status"] == "closed"
    with pytest.raises(RuntimeRefusal) as exc:  # so its next input is still refused
        rt.submit(t["thread_id"], "again", tenant="acme")
    assert exc.value.code == "thread_closed"


def test_a_session_closed_after_it_was_read_is_not_reactivated_by_input(tmp_path, monkeypatch):
    rt, _ = _rt(tmp_path, snapshot([manifest()]))
    t = rt.open_session("jobdoc", tenant="acme")
    stale = rt.store.get_thread(t["thread_id"], tenant="acme")
    rt.close_session(t["thread_id"], tenant="acme")
    monkeypatch.setattr(rt, "_thread", lambda tid, tenant: dict(stale))
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "x", tenant="acme")
    assert exc.value.code == "thread_closed"
    assert rt.store.get_thread(t["thread_id"])["status"] == "closed"
    assert rt.store.runs_for_thread(t["thread_id"], statuses=("pending",)) == []


# -- enqueue is bounded --------------------------------------------------------------------------


def test_enqueue_refuses_input_past_the_per_thread_backlog(tmp_path):
    from examlops.agent_runtime import runtime as rt_mod

    rt, _ = _rt(tmp_path, snapshot([manifest()]))  # the fixture manifest uses `enqueue`
    t = rt.open_session("jobdoc", tenant="acme")
    for i in range(rt_mod._MAX_QUEUED_PER_THREAD):
        rt.submit(t["thread_id"], f"in-{i}", tenant="acme")  # queued, never executed here
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "one too many", tenant="acme")
    assert exc.value.code == "thread_queue_full" and exc.value.status == 429
    waiting = rt.store.runs_for_thread(t["thread_id"], statuses=("pending",))
    assert len(waiting) == rt_mod._MAX_QUEUED_PER_THREAD


# -- a rollback's in-flight policy applies once, however many markers exist --------------------


def test_a_retirement_marker_is_found_behind_ten_thousand_others(tmp_path):
    v1 = manifest()
    v2 = manifest(prompts=[{"name": "jobdoc-system", "version": 8}])
    a, b = version_id_of(v1), version_id_of(v2)
    store = AgentStateStore(str(tmp_path / "agent_state.db"))
    rt, _ = _rt(tmp_path, snapshot([v1, v2], aliases={"Production": b}), store=store)
    t = rt.open_session("jobdoc", tenant="acme")  # a session on b

    def filler(conn):
        conn.executemany(
            "INSERT INTO rt_events (ts, kind, detail_json) VALUES (0, 'retire_applied', ?)",
            [(f'{{"key": "other:av-{i}:interrupt"}}',) for i in range(10_000)],
        )

    store._tx(filler)
    retire = {"aliases": {"Production": a}, "retired": {b: "interrupt"}}
    rt.apply_snapshot(snapshot([v1, v2], generation=2, **retire))  # applied once, marker #10001

    run = rt.submit(t["thread_id"], "after the rollback", tenant="acme")
    rt.apply_snapshot(snapshot([v1, v2], generation=3, **retire))  # an unrelated later snapshot
    assert rt.store.get_run(run["run_id"])["status"] == "pending"  # not interrupted again


# -- a blocking re-evaluation pin is never lost to a window -------------------------------------


def test_every_blocking_pin_is_read_however_many_are_open(monkeypatch):
    from examlops.agent_versions import reeval
    from examlops.data import agent_versions as avstore

    for i in range(3):
        avstore.enqueue_reeval(
            "jobdoc",
            f"av-sha256:{i:064d}",
            "qwen3-32b",
            "Production",
            "8",
            "7",
            blocking=True,
            actor="t",
        )
    real = avstore.open_blocking_reevals

    def windowed(**kw):  # a store that serves at most two rows per call
        return real(**{**kw, "limit": min(int(kw.get("limit", 10_000)), 2)})

    monkeypatch.setattr(avstore, "open_blocking_reevals", windowed)
    monkeypatch.setattr(reeval, "_BATCH", 2)
    pins = reeval.pinned_overrides()
    assert len(pins) == 3
    assert all(p == {"qwen3-32b@Production": "7"} for p in pins.values())
