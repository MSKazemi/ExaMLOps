"""The Agent Runtime (ADR 0144): runs, multitask strategies, durability, sessions, capability.

Real code end to end: the real runtime, the real SQLite agent state store, the real journaling
engine and - where a tool is called - the real tool broker over a small counted tool registry.
Each test asserts the outcome (what ran, how many times, what state a thread ends in), not argv.
"""

from __future__ import annotations

import threading
import time

import pytest

from examlops.agent_runtime import (
    AgentStateStore,
    BrokerGateway,
    Node,
    RuntimeRefusal,
    derive_idempotency_key,
)
from examlops.agent_runtime.routing import owner
from examlops.agent_versions.manifest import version_id_of
from tests.unit._agent_runtime_fixtures import (
    Crash,
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


def _basic(tools_calls: list | None = None):
    def plan(state, ctx):
        return {"plan": f"search:{state['input']}"}

    def act(state, ctx):
        res = ctx.call_tool("search_docs", {"q": state["input"]})
        return {"hits": res.get("hits")}

    def finish(state, ctx):
        return {"output": {"answer": state["hits"], "planner": ctx.model("planner")["version"]}}

    return linear_program(Node("plan", plan), Node("act", act), Node("finish", finish))


def _rt(tmp_path, m=None, **kw):
    m = m or manifest()
    tools = kw.pop("tools", None) or Tools()
    snap = kw.pop("snap", None) or snapshot([m])
    programs = kw.pop("programs", None) or {"tests.jobdoc:program": _basic()}
    rt = make_runtime(tmp_path, snap, programs, **kw)
    # The real broker, reading grants from the runtime's snapshot (not the live table).
    rt.gateway = BrokerGateway(tools=tools.specs, grant_resolver=rt._grants_for)
    return rt, tools, m


# -- a run, its checkpoints and its resolved models --------------------------------------------


def test_a_run_completes_checkpoints_every_step_and_records_resolved_models(tmp_path):
    rt, tools, m = _rt(tmp_path)
    t = rt.open_session("jobdoc", tenant="acme", principal="alice")
    run = rt.run_wait(t["thread_id"], "gpu", tenant="acme")
    assert run["status"] == "success"
    assert run["output"] == {"answer": ["GPU"], "planner": "7"}
    # Follow binding resolved ONCE at run start and recorded on the run (ADR 0146 d2).
    assert run["resolved_models"]["planner"] == {
        "servable": "gen://qwen3-32b",
        "binding": "follow",
        "alias": "Production",
        "version": "7",
        "source": "alias",
    }
    assert run["resolved_models"]["embedder"]["version"] == "4"
    cp = rt.store.latest_checkpoint(t["thread_id"])
    assert cp["agent_version_id"] == version_id_of(m)  # every checkpoint carries the version
    assert cp["next_node"] == "__end__" and run["steps"] == 4  # input + 3 nodes
    assert tools.count("search_docs") == 1


def test_a_follow_binding_with_no_resolvable_version_rejects_the_run(tmp_path):
    m = manifest()
    rt, _, _ = _rt(tmp_path, m, snap=snapshot([m], models={}))
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "x", tenant="acme")
    assert run["status"] == "rejected" and "unresolved_model" in run["error"]


def test_a_blocking_reeval_pin_holds_the_previous_model_version(tmp_path):
    m = manifest()
    vid = version_id_of(m)
    snap = snapshot([m], pins={vid: {"qwen3-32b@Production": "6"}})
    rt, _, _ = _rt(tmp_path, m, snap=snap)
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "x", tenant="acme")
    assert run["output"]["planner"] == "6"
    assert run["resolved_models"]["planner"]["source"] == "reeval_pin"


# -- durability: a crashed worker, a re-executed node, one side effect (verification 2) ---------


def test_a_crashed_run_resumes_on_another_worker_and_the_tool_runs_once(tmp_path):
    crashed = {"done": False}

    def submit(state, ctx):
        res = ctx.call_tool("submit_job", {"name": "train"})
        if not crashed["done"]:
            crashed["done"] = True
            raise Crash()  # the worker dies after the side effect, before the checkpoint
        return {"job": res["job_id"], "replayed": res.get("replayed", False)}

    def finish(state, ctx):
        return {"output": state["job"]}

    prog = linear_program(Node("submit", submit, idempotent=False), Node("finish", finish))
    m = manifest()
    tools = Tools()
    store = AgentStateStore(str(tmp_path / "agent_state.db"))
    rt1, _, _ = _rt(
        tmp_path,
        m,
        tools=tools,
        programs={"tests.jobdoc:program": prog},
        store=store,
        worker_id="w1",
        lease_ttl=0.2,
    )
    t = rt1.open_session("jobdoc", tenant="acme")
    run = rt1.submit(t["thread_id"], "go", tenant="acme")
    with pytest.raises(Crash):
        rt1.execute(run["run_id"])
    assert rt1.store.get_run(run["run_id"])["status"] == "running"  # orphaned
    time.sleep(0.25)  # the dead worker's lease expires

    rt2, _, _ = _rt(
        tmp_path,
        m,
        tools=tools,
        programs={"tests.jobdoc:program": prog},
        store=store,
        worker_id="w2",
    )
    recovered = rt2.recover()
    assert [r["status"] for r in recovered] == ["success"]
    assert tools.count("submit_job") == 1  # NOT executed twice
    assert recovered[0]["output"] == "job-1"
    replays = rt2.store.events(kind="tool_replayed")
    assert len(replays) == 1
    # The key is the ADR's derivation: the start checkpoint of the node, its name, call 0.
    cp_before = 1  # the run's input checkpoint
    assert replays[0]["detail"]["key"] == derive_idempotency_key(
        t["thread_id"], cp_before, "submit", 0
    )


def test_a_live_lease_is_not_recovered_by_another_worker(tmp_path):
    rt, _, _ = _rt(tmp_path)
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.submit(t["thread_id"], "x", tenant="acme")
    rt.store.update_run(run["run_id"], status="running")
    assert rt.store.acquire_lease(t["thread_id"], "someone-else", 60)
    assert rt.recover() == []


# -- human approval survives a restart and executes exactly once (ADR 0145 verification 4) ------


def test_a_tier_b_write_interrupts_survives_restart_and_executes_once(tmp_path):
    def act(state, ctx):
        res = ctx.call_tool("submit_job", {"name": "big"})
        return {"output": res}

    prog = linear_program(Node("act", act, idempotent=False))
    m = manifest()
    vid = version_id_of(m)
    grants = {vid: {"submit_job": {"schema_version": 1, "effect": "allow", "needs_approval": True}}}
    snap = snapshot([m], grants=grants)
    tools = Tools()
    store = AgentStateStore(str(tmp_path / "agent_state.db"))
    rt1, _, _ = _rt(
        tmp_path, m, snap=snap, tools=tools, programs={"tests.jobdoc:program": prog}, store=store
    )
    t = rt1.open_session("jobdoc", tenant="acme", principal="alice")
    run = rt1.run_wait(t["thread_id"], "go", tenant="acme")
    assert run["status"] == "interrupted"
    assert tools.count("submit_job") == 0
    intr = rt1.store.open_interrupt(run["run_id"])
    assert intr["kind"] == "approval" and intr["payload"]["tool"] == "submit_job"
    del rt1  # "restart": a new runtime process over the same state store

    rt2, _, _ = _rt(
        tmp_path, m, tools=tools, programs={"tests.jobdoc:program": prog}, store=store, snap=snap
    )
    done = rt2.resume(t["thread_id"], {"approve": True}, tenant="acme", by="bob")
    assert done["status"] == "success" and done["output"]["ok"] is True
    assert tools.count("submit_job") == 1
    with pytest.raises(RuntimeRefusal) as exc:  # the first answer stands
        rt2.resume(t["thread_id"], {"approve": True}, tenant="acme")
    assert exc.value.code == "no_interrupt"
    assert tools.count("submit_job") == 1


def test_a_rejected_approval_never_runs_the_tool(tmp_path):
    def act(state, ctx):
        return {"output": ctx.call_tool("submit_job", {"name": "big"})}

    prog = linear_program(Node("act", act))
    m = manifest()
    vid = version_id_of(m)
    grants = {vid: {"submit_job": {"schema_version": 1, "effect": "allow", "needs_approval": True}}}
    rt, tools, _ = _rt(
        tmp_path, m, snap=snapshot([m], grants=grants), programs={"tests.jobdoc:program": prog}
    )
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "go", tenant="acme")
    with pytest.raises(RuntimeRefusal) as bad:
        rt.resume(t["thread_id"], "yes please", tenant="acme")  # not {"approve": bool}
    assert bad.value.code == "bad_resume"
    done = rt.resume(t["thread_id"], {"approve": False}, tenant="acme")
    assert done["output"]["code"] == "approval_rejected"
    assert tools.count("submit_job") == 0


def test_an_input_interrupt_returns_the_human_answer_to_the_node(tmp_path):
    def ask(state, ctx):
        answer = ctx.interrupt({"question": "which partition?"})
        return {"output": f"partition={answer}"}

    rt, _, _ = _rt(tmp_path, programs={"tests.jobdoc:program": linear_program(Node("ask", ask))})
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "go", tenant="acme")
    assert run["status"] == "interrupted"
    done = rt.resume(t["thread_id"], "gpu", tenant="acme")
    assert done["status"] == "success" and done["output"] == "partition=gpu"


# -- multitask strategies (verification 6) ------------------------------------------------------


def _blocking_runtime(tmp_path, strategy):
    gate = threading.Event()
    entered = threading.Event()

    def slow(state, ctx):
        if state["input"] == "first":
            entered.set()
            assert gate.wait(120)
        return {"seen": [*state.get("seen", []), state["input"]]}

    def after(state, ctx):
        return {"output": list(state["seen"])}

    prog = linear_program(Node("slow", slow), Node("after", after))
    m = manifest(policy={"multitask_strategy": strategy})
    rt, _, _ = _rt(tmp_path, m, programs={"tests.jobdoc:program": prog})
    t = rt.open_session("jobdoc", tenant="acme")
    first = rt.submit(t["thread_id"], "first", tenant="acme")
    worker = threading.Thread(target=rt.execute, args=(first["run_id"],))
    worker.start()
    assert entered.wait(120)
    return rt, t, first, gate, worker


def test_reject_refuses_a_second_input_on_a_busy_thread(tmp_path):
    rt, t, first, gate, worker = _blocking_runtime(tmp_path, "reject")
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "second", tenant="acme")
    assert exc.value.code == "thread_busy" and exc.value.status == 409
    gate.set()
    worker.join(120)
    assert rt.store.get_run(first["run_id"])["status"] == "success"


def test_enqueue_runs_the_second_input_after_the_first(tmp_path):
    rt, t, first, gate, worker = _blocking_runtime(tmp_path, "enqueue")
    second = rt.submit(t["thread_id"], "second", tenant="acme")
    assert rt.execute(second["run_id"])["status"] == "pending"  # the lease holder will drain it
    gate.set()
    worker.join(120)
    assert rt.store.get_run(first["run_id"])["output"] == ["first"]
    assert rt.store.get_run(second["run_id"])["output"] == ["first", "second"]


def test_interrupt_stops_the_first_at_a_step_boundary_and_keeps_its_progress(tmp_path):
    rt, t, first, gate, worker = _blocking_runtime(tmp_path, "interrupt")
    second = rt.submit(t["thread_id"], "second", tenant="acme")
    gate.set()
    worker.join(120)
    r1 = rt.store.get_run(first["run_id"])
    assert r1["status"] == "superseded"
    # The first run's completed step stays in the state the second run builds on.
    assert rt.store.get_run(second["run_id"])["output"] == ["first", "second"]


def test_rollback_stops_the_first_and_discards_what_it_did(tmp_path):
    rt, t, first, gate, worker = _blocking_runtime(tmp_path, "rollback")
    second = rt.submit(t["thread_id"], "second", tenant="acme")
    gate.set()
    worker.join(120)
    assert rt.store.get_run(first["run_id"])["status"] == "rolled_back"
    assert rt.store.get_run(second["run_id"])["output"] == ["second"]  # first's step is gone


def test_budget_stops_a_run_that_exceeds_max_steps(tmp_path):
    def loop(state, ctx):
        return {"n": state.get("n", 0) + 1, "__next__": "loop"}

    m = manifest(budgets={"max_steps": 5})
    rt, _, _ = _rt(
        tmp_path, m, programs={"tests.jobdoc:program": linear_program(Node("loop", loop))}
    )
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "go", tenant="acme")
    assert run["status"] == "budget_exceeded" and run["steps"] == 5


# -- honest capability (verification 5) ----------------------------------------------------------


class _NotDurable:
    framework = "flaky"

    def capabilities(self):
        from examlops.agent_runtime import Capabilities

        return Capabilities(durable=False, cancel=True)

    def build(self, manifest, program, store):  # pragma: no cover - never reached
        raise AssertionError("a refused version must not be built")


def test_an_adapter_that_is_not_durable_is_refused_with_the_reason(tmp_path):
    from examlops.agent_runtime import register_adapter

    register_adapter(_NotDurable())
    m = manifest(
        code={
            "image": manifest()["code"]["image"],
            "entrypoint": "tests.jobdoc:program",
            "framework": "flaky",
        }
    )
    rt, _, _ = _rt(tmp_path, m)
    with pytest.raises(RuntimeRefusal) as exc:
        rt.deploy(version_id_of(m))
    assert exc.value.code == "capability_mismatch" and exc.value.status == 422
    assert "durable: false" in exc.value.reason
    with pytest.raises(RuntimeRefusal):  # and no session can be opened on it either
        rt.open_session("jobdoc", tenant="acme")
    assert rt.store.list_threads(tenant="acme") == []


def test_the_neutral_adapter_advertises_what_it_does(tmp_path):
    rt, _, m = _rt(tmp_path)
    caps = rt.deploy(version_id_of(m))["capabilities"]
    assert caps["durable"] and caps["interrupts"] and caps["idempotent_nodes"] and caps["rollback"]


def test_the_default_loader_refuses_code_outside_the_allow_list(tmp_path, monkeypatch):
    from examlops.agent_runtime import AgentRuntime, load_entrypoint

    monkeypatch.delenv("EXAMLOPS_AGENT_ENTRYPOINT_ALLOW", raising=False)
    with pytest.raises(RuntimeRefusal) as exc:
        load_entrypoint(manifest())
    assert exc.value.code == "entrypoint_not_allowed"
    m = manifest()
    rt = AgentRuntime(AgentStateStore(str(tmp_path / "s.db")), snapshot=snapshot([m]))
    with pytest.raises(RuntimeRefusal):
        rt.open_session("jobdoc", tenant="acme")


# -- sessions: lifecycle, suspension, admission, tenancy (verification 4) -----------------------


def test_idle_sessions_suspend_release_their_quota_and_resume_with_state(tmp_path):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])
    m = manifest()
    snap = snapshot([m], quotas={"default": {"max_sessions": 1}, "tenants": {}})
    rt, _, _ = _rt(tmp_path, m, snap=snap, store=store, idle_after=60, suspend_after=300)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "a", tenant="acme")
    with pytest.raises(RuntimeRefusal) as full:
        rt.open_session("jobdoc", tenant="acme")
    assert full.value.code == "session_quota_exceeded" and full.value.status == 429

    clock["t"] += 120
    assert rt.sweep() == {"idle": [t["thread_id"]], "suspended": []}
    clock["t"] += 400
    assert rt.sweep()["suspended"] == [t["thread_id"]]
    assert rt.usage("acme")["active_sessions"] == 0  # the released slot is visible
    other = rt.open_session("jobdoc", tenant="acme")  # ...and usable
    rt.close_session(other["thread_id"], tenant="acme")

    run = rt.run_wait(t["thread_id"], "b", tenant="acme")  # resumes from the state store
    assert run["status"] == "success"
    assert rt.store.get_thread(t["thread_id"])["status"] == "active"
    assert rt.store.events(thread_id=t["thread_id"], kind="session_resumed")


def test_another_tenants_thread_is_not_found(tmp_path):
    rt, _, _ = _rt(tmp_path)
    t = rt.open_session("jobdoc", tenant="acme")
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "x", tenant="globex")
    assert exc.value.status == 404
    assert rt.store.list_threads(tenant="globex") == []


def test_a_closed_session_refuses_input(tmp_path):
    rt, _, _ = _rt(tmp_path)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.close_session(t["thread_id"], tenant="acme")
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "x", tenant="acme")
    assert exc.value.code == "thread_closed"


def test_affinity_is_stable_and_moves_few_sessions_when_a_worker_leaves():
    workers = ["w1", "w2", "w3", "w4"]
    keys = [f"th-{i}" for i in range(2000)]
    before = {k: owner(k, workers) for k in keys}
    assert {owner("th-7", workers) for _ in range(5)} == {before["th-7"]}
    after = {k: owner(k, ["w1", "w2", "w3"]) for k in keys}
    moved = [k for k in keys if before[k] != after[k]]
    assert all(before[k] == "w4" for k in moved)  # only w4's sessions move
    assert 0.15 < len(moved) / len(keys) < 0.35


def test_input_size_is_bounded(tmp_path):
    rt, _, _ = _rt(tmp_path)
    t = rt.open_session("jobdoc", tenant="acme")
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(t["thread_id"], "x" * 300_000, tenant="acme")
    assert exc.value.status == 413


# -- static stability (verification 3) ------------------------------------------------------------


class _EchoGateway:
    def call(self, caller, tool, args, *, approved, idempotency_key, tenant):
        return {"ok": True, "hits": ["OFFLINE"]}


def test_the_runtime_serves_with_platform_db_down_from_its_last_known_good_snapshot(
    tmp_path, monkeypatch
):
    m = manifest()
    store = AgentStateStore(str(tmp_path / "agent_state.db"))
    first, _, _ = _rt(tmp_path, m, store=store)
    t_old = first.open_session("jobdoc", tenant="acme")
    del first

    # platform.db, the control plane and MLflow are gone.
    import examlops.platform_db as pdb

    def down(*_a, **_k):
        raise RuntimeError("platform.db unavailable")

    monkeypatch.setattr(pdb, "get_db", down)
    monkeypatch.setattr(pdb, "init_db", down)

    from examlops.agent_runtime import AgentRuntime

    rt = AgentRuntime(
        store, gateway=_EchoGateway(), program_loader=lambda _m: _basic()
    )  # no snapshot passed: last-known-good
    assert rt.snapshot is not None
    assert rt.run_wait(t_old["thread_id"], "a", tenant="acme")["status"] == "success"
    t_new = rt.open_session("jobdoc", tenant="acme")
    assert rt.run_wait(t_new["thread_id"], "b", tenant="acme")["output"]["answer"] == ["OFFLINE"]


def test_an_older_or_tampered_snapshot_is_not_adopted(tmp_path):
    m = manifest()
    rt, _, _ = _rt(tmp_path, m, snap=snapshot([m], generation=5))
    assert rt.apply_snapshot(snapshot([m], generation=4)) is False
    assert rt.snapshot["generation"] == 5
    bad = snapshot([m], generation=9)
    bad["models"] = {"qwen3-32b": {"Production": "666"}}  # edited after compile
    with pytest.raises(ValueError, match="digest"):
        rt.apply_snapshot(bad)


def test_the_state_store_refuses_to_be_platform_db(tmp_path, monkeypatch):
    from examlops.agent_runtime.store import default_path

    p = str(tmp_path / "platform.db")
    monkeypatch.setenv("PLATFORM_DB", p)
    monkeypatch.setenv("EXAMLOPS_AGENT_STATE_DB", p)
    with pytest.raises(ValueError, match="must not be platform.db"):
        default_path()


def test_prune_bounds_recorded_tool_results(tmp_path):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])
    rt, _, _ = _rt(tmp_path, store=store)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "a", tenant="acme")
    rt.close_session(t["thread_id"], tenant="acme")
    clock["t"] += 10 * 86400
    out = store.prune(older_than_s=7 * 86400)
    assert out["tool_results"] == 1 and out["threads"] == 1
    assert store.get_thread(t["thread_id"]) is None


# -- review fixes: the lease is what makes "a side effect runs once" true ----------------------


def _slow_tool_runtime(tmp_path, *, lease_ttl):
    """A runtime whose first ``submit_job`` blocks until released (a slow external system)."""
    from examlops.mcp.tools import ToolSpec

    calls: list[str] = []
    started, release = threading.Event(), threading.Event()

    def submit_job(name: str = "job") -> dict:
        calls.append(name)
        if len(calls) == 1:
            started.set()
            assert release.wait(30)
        return {"ok": True, "job_id": f"job-{len(calls)}"}

    def act(state, ctx):
        return {"output": ctx.call_tool("submit_job", {"name": "train"})}

    prog = linear_program(Node("act", act, idempotent=False))
    store = AgentStateStore(str(tmp_path / "agent_state.db"))
    rt, _, _ = _rt(
        tmp_path,
        programs={"tests.jobdoc:program": prog},
        store=store,
        worker_id="w1",
        lease_ttl=lease_ttl,
    )
    rt.gateway = BrokerGateway(
        tools={"submit_job": ToolSpec(fn=submit_job, mutating=True, tier="B")},
        grant_resolver=rt._grants_for,
    )
    return rt, store, calls, started, release


def test_a_tool_call_slower_than_the_lease_is_not_repeated_by_recovery(tmp_path):
    # Before the fix the lease was renewed only at checkpoints: a node busy in one tool call for
    # longer than the TTL looked dead, another worker's recovery re-executed the node, found no
    # journaled result (the first call had not returned) and called the tool a second time.
    rt, store, calls, started, release = _slow_tool_runtime(tmp_path, lease_ttl=0.3)
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.submit(t["thread_id"], "go", tenant="acme")
    worker = threading.Thread(target=rt.execute, args=(run["run_id"],))
    worker.start()
    assert started.wait(30)
    time.sleep(0.9)  # three TTLs inside one tool call

    other = make_runtime(
        tmp_path,
        rt.snapshot,
        {"tests.jobdoc:program": rt.program_loader(rt._manifest(run["version_id"]))},
        store=store,
        worker_id="w2",
    )
    other.gateway = rt.gateway
    assert other.recover() == []  # the slow worker is alive and still holds the thread
    release.set()
    worker.join(30)
    assert calls == ["train"]
    assert store.get_run(run["run_id"])["status"] == "success"


def test_a_worker_that_lost_its_lease_does_not_perform_the_side_effect(tmp_path):
    tools = Tools()
    stolen = {"done": False}

    def act(state, ctx):
        if not stolen["done"]:  # another worker takes the thread over (our lease had lapsed)
            stolen["done"] = True
            ctx._hooks.rt.store._tx(
                lambda c: c.execute("UPDATE rt_leases SET holder='w2#1', expires_at=1e18")
            )
        return {"output": ctx.call_tool("submit_job", {"name": "train"})}

    rt, _, _ = _rt(
        tmp_path, tools=tools, programs={"tests.jobdoc:program": linear_program(Node("act", act))}
    )
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "go", tenant="acme")
    assert tools.count("submit_job") == 0
    assert run["status"] == "running"  # left for the worker that now owns it
    assert [e["run_id"] for e in rt.store.events(kind="run_lease_lost")] == [run["run_id"]]


def test_an_orphan_is_found_behind_a_window_of_live_runs(tmp_path):
    rt, _, _ = _rt(tmp_path)
    store = rt.store
    live = []
    for i in range(3):
        t = rt.open_session("jobdoc", tenant="acme")
        r = rt.submit(t["thread_id"], f"live-{i}", tenant="acme")
        store.update_run(r["run_id"], status="running")
        assert store.acquire_lease(t["thread_id"], f"busy-{i}", 600)
        live.append(r["run_id"])
    t = rt.open_session("jobdoc", tenant="acme")
    orphan = rt.submit(t["thread_id"], "orphan", tenant="acme")
    store.update_run(orphan["run_id"], status="running")  # updated last: at the END of the order
    got = store.orphaned_runs(now=store.clock(), limit=1)
    assert [r["run_id"] for r in got] == [orphan["run_id"]]


def test_prune_keeps_the_journal_of_a_run_that_can_still_re_execute(tmp_path):
    clock = {"t": 1000.0}
    store = AgentStateStore(str(tmp_path / "agent_state.db"), clock=lambda: clock["t"])

    def act(state, ctx):
        res = ctx.call_tool("search_docs", {"q": "x"})
        answer = ctx.interrupt({"question": "go on?"})
        return {"output": [res["hits"], answer]}

    rt, tools, _ = _rt(
        tmp_path, store=store, programs={"tests.jobdoc:program": linear_program(Node("a", act))}
    )
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], "go", tenant="acme")
    assert run["status"] == "interrupted"  # parked on a human for longer than the retention
    clock["t"] += 10 * 86400
    assert store.prune(older_than_s=7 * 86400)["tool_results"] == 0
    done = rt.resume(t["thread_id"], "yes", tenant="acme")
    assert done["status"] == "success"
    assert tools.count("search_docs") == 1  # the re-executed node was answered by the journal


def test_an_explicit_state_store_path_may_not_be_platform_db(tmp_path, monkeypatch):
    p = str(tmp_path / "platform.db")
    monkeypatch.setenv("PLATFORM_DB", p)
    with pytest.raises(ValueError, match="must not be platform.db"):
        AgentStateStore(p)
