"""The runtime's HTTP surface and framework neutrality (ADR 0144 decision 2, verification 1).

One LangGraph agent and one plain-Python agent, deployed from two agent versions onto the SAME
runtime, each complete a run, an interrupt and a resume through the same HTTP surface.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, TypedDict

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from examlops.agent_runtime import Node  # noqa: E402
from examlops.agent_runtime.http import create_app  # noqa: E402
from examlops.agent_versions.manifest import version_id_of  # noqa: E402
from tests.unit._agent_runtime_fixtures import (  # noqa: E402
    IMAGE,
    linear_program,
    make_runtime,
    manifest,
    snapshot,
)

TOKENS = {
    "acme-token-0123456789": {"subject": "alice", "tenant": "acme"},
    "globex-token-987654321": {"subject": "mallory", "tenant": "globex"},
}
ACME = {"Authorization": "Bearer acme-token-0123456789"}
GLOBEX = {"Authorization": "Bearer globex-token-987654321"}


def _python_agent():
    def ask(state, ctx):
        answer = ctx.interrupt({"question": "which partition?"})
        return {"output": f"python:{state['input']}:{answer}"}

    return linear_program(Node("ask", ask))


def _langgraph_agent():
    lg = pytest.importorskip("langgraph.graph")
    types = pytest.importorskip("langgraph.types")
    pytest.importorskip("langgraph.checkpoint.sqlite")

    class S(TypedDict, total=False):
        input: str
        log: Annotated[list, operator.add]
        output: str

    def plan(state: S) -> dict:
        return {"log": ["plan"]}

    def ask(state: S) -> dict:
        answer = types.interrupt({"question": "approve?"})
        return {"log": ["ask"], "output": f"langgraph:{state['input']}:{answer}"}

    g = lg.StateGraph(S)
    g.add_node("plan", plan)
    g.add_node("ask", ask)
    g.add_edge(lg.START, "plan")
    g.add_edge("plan", "ask")
    g.add_edge("ask", lg.END)
    return g


def _two_agent_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AGENT_RUNTIME_TOKENS", json.dumps(TOKENS))
    py = manifest()
    lgm = manifest(
        agent="jobdoc",
        code={"image": IMAGE, "entrypoint": "tests.lg:graph", "framework": "langgraph"},
        prompts=[{"name": "jobdoc-system", "version": 8}],
    )
    snap = snapshot(
        [py, lgm], aliases={"Production": version_id_of(py), "Staging": version_id_of(lgm)}
    )
    rt = make_runtime(
        tmp_path,
        snap,
        {"tests.jobdoc:program": _python_agent(), "tests.lg:graph": _langgraph_agent()},
    )
    return rt, TestClient(create_app(rt))


def test_two_frameworks_run_interrupt_and_resume_through_one_http_surface(tmp_path, monkeypatch):
    rt, client = _two_agent_runtime(tmp_path, monkeypatch)
    for alias, expect in (("Production", "python:hi:gpu"), ("Staging", "langgraph:hi:gpu")):
        t = client.post("/threads", json={"agent": "jobdoc", "alias": alias}, headers=ACME)
        assert t.status_code == 200, t.text
        tid = t.json()["thread_id"]
        r = client.post(
            f"/threads/{tid}/runs/wait",
            json={"input": {"input": "hi"}} if alias == "Staging" else {"input": "hi"},
            headers=ACME,
        )
        assert r.json()["status"] == "interrupted", r.text
        done = client.post(
            f"/threads/{tid}/runs/wait", json={"command": {"resume": "gpu"}}, headers=ACME
        )
        assert done.status_code == 200 and done.json()["status"] == "success", done.text
        assert done.json()["output"] == expect
        state = client.get(f"/threads/{tid}/state", headers=ACME).json()
        assert state["values"]["output"] == expect


def test_langgraph_checkpoints_carry_the_version_and_rollback_is_honestly_refused(
    tmp_path, monkeypatch
):
    rt, client = _two_agent_runtime(tmp_path, monkeypatch)
    lg_vid = rt.snapshot["agents"]["jobdoc"]["aliases"]["Staging"]
    t = rt.open_session("jobdoc", tenant="acme", alias="Staging")
    rt.run_wait(t["thread_id"], {"input": "x"}, tenant="acme")
    assert rt.get_state(t["thread_id"], tenant="acme").agent_version_id == lg_vid
    caps = rt.deploy(lg_vid)["capabilities"]
    assert caps["durable"] and not caps["rollback"] and not caps["idempotent_nodes"]

    from examlops.agent_runtime import RuntimeRefusal

    bad = manifest(
        code={"image": IMAGE, "entrypoint": "tests.lg:graph", "framework": "langgraph"},
        policy={"multitask_strategy": "rollback"},
    )
    rt.apply_snapshot(snapshot([bad], generation=99))
    with pytest.raises(RuntimeRefusal) as exc:
        rt.deploy(version_id_of(bad))
    assert exc.value.code == "capability_mismatch" and "rollback" in exc.value.reason


def test_auth_fails_closed_and_tenancy_comes_from_the_credential(tmp_path, monkeypatch):
    rt, client = _two_agent_runtime(tmp_path, monkeypatch)
    assert client.post("/threads", json={"agent": "jobdoc"}).status_code == 401
    assert (
        client.post(
            "/threads", json={"agent": "jobdoc"}, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    tid = client.post("/threads", json={"agent": "jobdoc"}, headers=ACME).json()["thread_id"]
    assert client.get(f"/threads/{tid}", headers=GLOBEX).status_code == 404
    assert (
        client.post(f"/threads/{tid}/runs/wait", json={"input": "x"}, headers=GLOBEX).status_code
        == 404
    )
    assert client.get("/threads", headers=GLOBEX).json() == []

    monkeypatch.setenv("EXAMLOPS_AGENT_RUNTIME_TOKENS", json.dumps({"changeme": {"tenant": "x"}}))
    r = client.get("/assistants", headers={"Authorization": "Bearer changeme"})
    assert r.status_code == 503 and r.json()["code"] == "auth_unconfigured"
    monkeypatch.delenv("EXAMLOPS_AGENT_RUNTIME_TOKENS")
    assert client.get("/assistants", headers=ACME).status_code == 503


def test_busy_thread_refusal_maps_to_409_and_affinity_to_421(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AGENT_RUNTIME_TOKENS", json.dumps(TOKENS))
    m = manifest(policy={"multitask_strategy": "reject"})
    rt = make_runtime(
        tmp_path,
        snapshot([m]),
        {"tests.jobdoc:program": _python_agent()},
        worker_id="w1",
        peers=["w1", "w2", "w3"],
    )
    client = TestClient(create_app(rt))
    mine = other = None
    for _ in range(60):
        t = client.post("/threads", json={"agent": "jobdoc"}, headers=ACME).json()
        if rt.owner(t["thread_id"]) == "w1":
            mine = mine or t["thread_id"]
        else:
            other = other or t["thread_id"]
        if mine and other:
            break
    r = client.post(f"/threads/{other}/runs/wait", json={"input": "x"}, headers=ACME)
    assert r.status_code == 421 and r.json()["code"] == "misdirected"
    assert (
        client.post(f"/threads/{mine}/runs/wait", json={"input": "x"}, headers=ACME).json()[
            "status"
        ]
        == "interrupted"
    )  # parked = busy
    r = client.post(f"/threads/{mine}/runs/wait", json={"input": "y"}, headers=ACME)
    assert r.status_code == 409 and r.json()["code"] == "thread_busy"
    assert client.get("/assistants", headers=ACME).json()[0]["assistant_id"] == "jobdoc"


def test_a_langgraph_agent_is_held_to_its_step_budget(tmp_path):
    # LangGraph checkpoints through its own saver, so the runtime's checkpoint hook - where the
    # budget was counted - never ran for it: `budgets.max_steps` silently did nothing.
    lg = pytest.importorskip("langgraph.graph")
    pytest.importorskip("langgraph.checkpoint.sqlite")

    class S(TypedDict, total=False):
        input: str
        log: Annotated[list, operator.add]
        output: str

    g = lg.StateGraph(S)
    for name in ("a", "b", "c", "d"):
        g.add_node(name, lambda state, _n=name: {"log": [_n], "output": _n})
    g.add_edge(lg.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", "d")
    g.add_edge("d", lg.END)
    m = manifest(
        code={"image": IMAGE, "entrypoint": "tests.lg:graph", "framework": "langgraph"},
        budgets={"max_steps": 2},
    )
    rt = make_runtime(tmp_path, snapshot([m]), {"tests.lg:graph": g})
    t = rt.open_session("jobdoc", tenant="acme")
    run = rt.run_wait(t["thread_id"], {"input": "x"}, tenant="acme")
    assert run["status"] == "budget_exceeded" and run["steps"] == 2
    assert rt.get_state(t["thread_id"], tenant="acme").values["log"] == ["a", "b"]


def test_a_token_that_names_no_tenant_is_a_misconfiguration_not_a_shared_default(
    tmp_path, monkeypatch
):
    # Before the fix an entry without "tenant" was silently placed in tenant "default", so two
    # such tokens - issued to different parties - could read each other's threads.
    rt, client = _two_agent_runtime(tmp_path, monkeypatch)
    loose = "loose-token-0123456789"
    bearer = {"Authorization": f"Bearer {loose}"}
    monkeypatch.setenv("EXAMLOPS_AGENT_RUNTIME_TOKENS", json.dumps({loose: {"subject": "bob"}}))
    r = client.post("/threads", json={"agent": "jobdoc"}, headers=bearer)
    assert r.status_code == 503 and r.json()["code"] == "auth_unconfigured"
    assert rt.store.list_threads(tenant=None) == []
    for bad in ({loose: "bob"}, [loose]):  # malformed maps fail closed with a reason, not a 500
        monkeypatch.setenv("EXAMLOPS_AGENT_RUNTIME_TOKENS", json.dumps(bad))
        r = client.get("/assistants", headers=bearer)
        assert r.status_code == 503 and r.json()["code"] == "auth_unconfigured"


def test_an_oversized_body_is_refused(tmp_path, monkeypatch):
    rt, client = _two_agent_runtime(tmp_path, monkeypatch)
    tid = client.post("/threads", json={"agent": "jobdoc"}, headers=ACME).json()["thread_id"]
    big = json.dumps({"input": "x" * (600 * 1024)})
    r = client.post(
        f"/threads/{tid}/runs/wait",
        content=big,
        headers={**ACME, "Content-Type": "application/json"},
    )
    assert r.status_code == 413 and r.json()["code"] == "body_too_large"
    assert rt.store.runs_for_thread(tid) == []
