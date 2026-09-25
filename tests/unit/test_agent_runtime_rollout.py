"""ADR 0146 decision 4 inside the runtime: session canary, pinned sessions, state migrations,
rollback in-flight policies, and replay shadow with no live side effects."""

from __future__ import annotations

import pytest

from examlops.agent_runtime import BrokerGateway, Node, RuntimeRefusal
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


def _prog(tag):
    def act(state, ctx):
        return {"output": f"{tag}:{state['input']}"}

    return linear_program(Node("act", act))


def _two(**over):
    v1 = manifest()
    v2 = manifest(
        prompts=[{"name": "jobdoc-system", "version": 8}],
        code={**v1["code"], "entrypoint": "tests.jobdoc:v2"},
        **over,
    )
    return v1, v2


PROGRAMS = {"tests.jobdoc:program": _prog("v1"), "tests.jobdoc:v2": _prog("v2")}


def test_ten_percent_of_new_sessions_start_on_canary_and_none_ever_move(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    snap = snapshot(
        [v1, v2],
        aliases={"Production": a, "Canary": b},
        canary_percent=10,
        quotas={"default": {"max_sessions": 10_000}, "tenants": {}},
    )
    rt = make_runtime(tmp_path, snap, PROGRAMS)
    threads = [rt.open_session("jobdoc", tenant="acme") for _ in range(600)]
    on_canary = [t for t in threads if t["version_id"] == b]
    assert all(t["canary"] for t in on_canary)
    assert 0.05 < len(on_canary) / len(threads) < 0.15  # 10% +- ~3 standard errors at n=600

    # The share changes: zero existing sessions change version.
    rt.apply_snapshot(
        snapshot(
            [v1, v2],
            aliases={"Production": a, "Canary": b},
            canary_percent=90,
            generation=2,
            quotas={"default": {"max_sessions": 10_000}, "tenants": {}},
        )
    )
    for t in threads[:40]:
        run = rt.run_wait(t["thread_id"], "x", tenant="acme")
        assert run["output"] == ("v2:x" if t["version_id"] == b else "v1:x")
        assert rt.store.get_thread(t["thread_id"])["version_id"] == t["version_id"]


def _interrupting(tag):
    def ask(state, ctx):
        return {"output": f"{tag}:{ctx.interrupt({'q': 'go?'})}"}

    return linear_program(Node("ask", ask))


def test_with_pin_an_interrupted_thread_resumes_on_the_old_version(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    programs = {"tests.jobdoc:program": _interrupting("v1"), "tests.jobdoc:v2": _interrupting("v2")}
    rt = make_runtime(tmp_path, snapshot([v1, v2], aliases={"Production": a}), programs)
    t = rt.open_session("jobdoc", tenant="acme")
    assert rt.run_wait(t["thread_id"], "x", tenant="acme")["status"] == "interrupted"
    # Production moves to v2 with an incompatible schema, promoted with `pin`.
    rt.apply_snapshot(
        snapshot(
            [v1, v2],
            aliases={"Production": b},
            generation=2,
            migrations={a: {"to": b, "outcome": "pin"}},
        )
    )
    done = rt.resume(t["thread_id"], "yes", tenant="acme")
    assert done["output"] == "v1:yes" and done["version_id"] == a
    rt.run_wait(t["thread_id"], "again", tenant="acme")
    assert rt.store.get_thread(t["thread_id"])["version_id"] == a  # pinned until it closes
    assert rt.open_session("jobdoc", tenant="acme")["version_id"] == b  # new sessions: v2


def test_compatible_and_drain_move_old_threads_to_the_new_version(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    rt = make_runtime(tmp_path, snapshot([v1, v2], aliases={"Production": a}), PROGRAMS)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.run_wait(t["thread_id"], "x", tenant="acme")
    rt.apply_snapshot(
        snapshot(
            [v1, v2],
            aliases={"Production": b},
            generation=2,
            migrations={a: {"to": b, "outcome": "drain"}},
        )
    )
    assert rt.run_wait(t["thread_id"], "y", tenant="acme")["output"] == "v2:y"
    assert rt.store.events(thread_id=t["thread_id"], kind="thread_migrated")


def test_quarantine_stops_and_locks_sessions_on_the_rolled_back_version(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    programs = {"tests.jobdoc:program": _prog("v1"), "tests.jobdoc:v2": _interrupting("v2")}
    rt = make_runtime(tmp_path, snapshot([v1, v2], aliases={"Production": b}), programs)
    bad = rt.open_session("jobdoc", tenant="acme")
    parked = rt.run_wait(bad["thread_id"], "x", tenant="acme")
    assert parked["status"] == "interrupted"
    # `exa agent alias rollback jobdoc Production --in-flight quarantine` reaches the runtime:
    rt.apply_snapshot(
        snapshot([v1, v2], aliases={"Production": a}, generation=2, retired={b: "quarantine"})
    )
    assert rt.store.get_thread(bad["thread_id"])["status"] == "quarantined"
    assert rt.store.get_run(parked["run_id"])["status"] == "cancelled"
    with pytest.raises(RuntimeRefusal) as exc:
        rt.submit(bad["thread_id"], "y", tenant="acme")
    assert exc.value.code == "thread_quarantined" and exc.value.status == 423
    fresh = rt.open_session("jobdoc", tenant="acme")
    assert rt.run_wait(fresh["thread_id"], "z", tenant="acme")["output"] == "v1:z"
    # Re-applying the same snapshot does not act twice.
    rt.apply_snapshot(
        snapshot([v1, v2], aliases={"Production": a}, generation=3, retired={b: "quarantine"})
    )
    assert len(rt.store.events(kind="retire_applied")) == 1


def test_continue_leaves_in_flight_sessions_alone(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    rt = make_runtime(tmp_path, snapshot([v1, v2], aliases={"Production": b}), PROGRAMS)
    t = rt.open_session("jobdoc", tenant="acme")
    rt.apply_snapshot(
        snapshot([v1, v2], aliases={"Production": a}, generation=2, retired={b: "continue"})
    )
    assert rt.run_wait(t["thread_id"], "x", tenant="acme")["output"] == "v2:x"


# -- replay shadow (verification 5) ---------------------------------------------------------------


class CountingGateway(BrokerGateway):
    def __init__(self, tools):
        super().__init__(tools=tools.specs)
        self.n = 0

    def call(self, *a, **k):
        self.n += 1
        return super().call(*a, **k)


def _agent(tag, extra_tool=False):
    def act(state, ctx):
        job = ctx.call_tool("submit_job", {"name": state["input"]})
        hits = ctx.call_tool("search_docs", {"q": state["input"]})
        if extra_tool:
            ctx.call_tool("submit_job", {"name": "surprise"})  # not in the recording
        return {"output": f"{tag}:{job.get('job_id')}:{hits.get('hits')}"}

    return linear_program(Node("act", act, idempotent=False))


def test_replay_runs_the_candidate_on_recorded_results_and_never_reaches_a_live_tool(tmp_path):
    v1, v2 = _two()
    a, b = version_id_of(v1), version_id_of(v2)
    tools = Tools()
    programs = {
        "tests.jobdoc:program": _agent("v1"),
        "tests.jobdoc:v2": _agent("v1", extra_tool=True),
    }
    rt = make_runtime(tmp_path, snapshot([v1, v2], aliases={"Production": a}), programs)
    gw = CountingGateway(tools)
    rt.gateway = gw
    t = rt.open_session("jobdoc", tenant="acme")
    for word in ("alpha", "beta"):
        assert rt.run_wait(t["thread_id"], word, tenant="acme")["status"] == "success"
    live_before, side_effects_before = gw.n, tools.count("submit_job")
    assert live_before == 4 and side_effects_before == 2

    report = rt.replay(t["thread_id"], b, tenant="acme")
    assert gw.n == live_before and tools.count("submit_job") == side_effects_before  # no live call
    assert report["live_calls"] == 0 and report["matched"] == 4
    assert [u["tool"] for u in report["not_recorded"]] == ["submit_job", "submit_job"]
    assert report["equal_outputs"] == 2  # same trajectory otherwise -> same answers
    # The scratch thread is gone; the recorded session is untouched.
    assert [x["thread_id"] for x in rt.store.list_threads(tenant="acme")] == [t["thread_id"]]
    with pytest.raises(RuntimeRefusal):
        rt.replay(t["thread_id"], b, tenant="globex")
