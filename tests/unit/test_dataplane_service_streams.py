"""ADR 0131 — the dataplane service's stream surface (dataplane Plan 2 task A8).

The push route (``POST /streams/{name}/messages``) over Plan 1's auth with the new ``ingest``
scope, the body cap (by Content-Length and by a chunked body), every status code as an RFC 9457
problem document, idempotent replays, stream states, the read routes, the process roles and the
drain order. The ingress stack and the supervisor are fakes: this file tests the service, not the
ingress (``test_dataplane_streams_ingress.py``) or the supervisor
(``test_dataplane_streams_supervisor.py``).

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import inspect
import json
import logging
import signal
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from examlops import iam as iam_mod
from examlops.data.dataplane import set_stream_state, upsert_stream
from examlops.dataplane.service import app as app_mod
from examlops.dataplane.service.app import PROBLEM_TYPE_BASE, create_app
from examlops.dataplane.service.auth import (
    INGEST_DISABLED_DETAIL,
    STREAM_FORBIDDEN_DETAIL,
    authenticate_ingest,
    authenticate_read,
    authenticate_write,
    require_read,
)
from examlops.dataplane.streams.ingress import IngressResult
from examlops.dataplane.streams.supervisor import IngressStack

TOKEN = "a-real-dataplane-token-0123456789"
INGEST = "an-ingest-only-token-9876543210"
H = {"Authorization": f"Bearer {TOKEN}"}
HI = {"Authorization": f"Bearer {INGEST}"}
SECRET = "payload-secret-hunter2"
OK = IngressResult(outcome="ok", prediction=0.75, body={"prediction": 0.75, "model_version": "7"})


# ── fakes ───────────────────────────────────────────────────────────────────────────────────


class FakeIngress:
    """Replies with a scripted result, recording every request."""

    def __init__(self) -> None:
        self.result: Any = OK
        self.calls: list[tuple[Any, Any]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def handle(self, binding, req, *, reply=None):
        self.calls.append((binding, req))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if reply is not None:
            reply(self.result)
        return self.result

    def stats(self):
        return {"proj/push1": {"requests": 3, "outcomes": {"ok": 3}}}


class FakeSupervisor:
    def __init__(self, log: list[str], rt_ref: dict[str, Any]) -> None:
        self.log = log
        self.rt_ref = rt_ref
        self.started = False
        self.stop_timeout: float | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float) -> None:
        rt = self.rt_ref.get("rt")
        self.log.append(f"supervisor.stop draining={rt.draining.is_set() if rt else None}")
        self.stop_timeout = timeout

    def release_leases(self, timeout: float | None = None) -> None:
        self.log.append("supervisor.release_leases")

    def status_of(self, project, name):
        return {"project": project, "name": name, "state": "running", "leader": True}


class Part:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    def close(self, timeout: float | None = None) -> None:
        self.log.append(self.name)


@pytest.fixture(autouse=True)
def _iam_off(monkeypatch, tmp_path):
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=False))
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    for var in (
        "EXAMLOPS_DATAPLANE_ROLE",
        "EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES",
        "EXAMLOPS_MULTITENANCY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATAPLANE_TOKEN", TOKEN)
    monkeypatch.setenv("DATAPLANE_INGEST_TOKEN", INGEST)


def _stream(project: str, name: str, *, connector: str = "http", state: str = "enabled", **kw):
    upsert_stream(
        project,
        name,
        connector=connector,
        model=kw.pop("model", "JPCP"),
        alias="Production",
        address=kw.pop("address", ""),
        connection=None,
        options=kw.pop("options", {}),
        limits=kw.pop("limits", {}),
        state=state,
        origin="api",
        actor=None,
    )


class Env(SimpleNamespace):
    client: TestClient
    ingress: FakeIngress
    log: list[str]
    supervisor: FakeSupervisor | None
    app: Any


@pytest.fixture
def make():
    """Build the app (streams on) with fake stack/supervisor; yields a factory."""
    opened: list[TestClient] = []

    def _make(**kw: Any) -> Env:
        log: list[str] = []
        ingress = FakeIngress()
        rt_ref: dict[str, Any] = {}
        made: dict[str, Any] = {}

        def stack_factory():
            return IngressStack(
                ingress,
                spool=Part("spool", log),
                drift=Part("drift", log),
                client=Part("client", log),
            )

        def supervisor_factory(ing, view):
            made["sup"] = FakeSupervisor(log, rt_ref)
            return made["sup"]

        app = create_app(
            start_scheduler=False,
            streams=True,
            stack_factory=stack_factory,
            supervisor_factory=supervisor_factory,
            **kw,
        )
        rt_ref["rt"] = app.state.streams
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return Env(client=client, ingress=ingress, log=log, supervisor=made.get("sup"), app=app)

    yield _make
    for c in opened:
        try:
            c.__exit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
def env(make):
    _stream("", "push1")
    _stream("proj", "push1")
    _stream("", "paused1", state="paused")
    _stream("", "off1", state="disabled")
    _stream("", "kafka1", connector="kafka", address="topic-x")
    return make()


def _push(client, name="push1", *, body: Any = None, headers=None, project=None, **kw):
    params = {"project": project} if project is not None else None
    content = kw.pop("content", None)
    if content is None:
        content = json.dumps(body if body is not None else {"payload": {"x": 1, "note": SECRET}})
    return client.post(
        f"/streams/{name}/messages",
        content=content,
        headers={**(headers if headers is not None else H), "Content-Type": "application/json"},
        params=params,
        **kw,
    )


def _is_problem(r, status: int, slug: str | None = None) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    doc = r.json()
    assert doc["status"] == status and doc["title"] and isinstance(doc["detail"], str)
    if slug is not None:
        assert doc["type"] == PROBLEM_TYPE_BASE + slug
    assert SECRET not in r.text  # never the payload
    return doc


# ── the happy path ──────────────────────────────────────────────────────────────────────────


def test_a_push_is_served_and_answers_the_prediction(env):
    r = _push(env.client, headers={**H, "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True,
        "prediction": 0.75,
        "model": "JPCP",
        "version": "7",
        "outcome": "ok",
    }
    assert "Idempotent-Replayed" not in r.headers
    binding, req = env.ingress.calls[0]
    assert (binding.project, binding.name, binding.connector) == ("", "push1", "http")
    assert req.payload == {"x": 1, "note": SECRET} and req.stream == "push1"
    assert req.traceparent == "00-" + "a" * 32 + "-" + "b" * 16 + "-01"


def test_the_envelope_rule_is_the_kafka_one(env):
    _push(env.client, body={"x": 2})  # a bare object is the payload
    body = {"payload": {"x": 3}, "alias": "Canary", "metadata": {"job_id": "j1", "tenant": "evil"}}
    _push(env.client, body=body)
    (_, bare), (_, enveloped) = env.ingress.calls
    assert bare.payload == {"x": 2} and bare.alias == "" and bare.metadata == {}
    assert enveloped.payload == {"x": 3} and enveloped.alias == "Canary"
    assert enveloped.metadata == {"job_id": "j1"}  # a message can never set the tenant
    for bad in (b"not json", b"[1, 2]", b'{"payload": {"x": 1}, "metadata": "nope"}', b""):
        _is_problem(_push(env.client, content=bad), 422, "validation")
    assert len(env.ingress.calls) == 2


def test_an_alias_the_message_names_is_refused_end_to_end(make):
    """C1: the route parses the envelope's ``alias`` and hands it on verbatim; the ingress — the
    one authorization point — refuses it, and the push answers 422. Asserted here against the
    REAL ingress, since this file's ``FakeIngress`` would serve anything."""
    from examlops.dataplane.streams.ingress import StreamIngress

    _stream("", "push1")  # bound to alias Production (see `_stream`)
    e = make()
    r = _push(e.client, body={"payload": {"x": 1}, "alias": "Canary"})
    assert r.status_code == 200  # the fake ingress serves it...
    (binding, req) = e.ingress.calls[0]
    assert req.alias == "Canary" and binding.alias == "Production"

    class _Client:
        calls: list[Any] = []

        def infer(self, request, body):
            _Client.calls.append(request)
            raise AssertionError("the ingress must refuse before inference")

    class _Spool:
        def offer(self, record):  # pragma: no cover - never reached
            return True

    result = StreamIngress(_Client(), _Spool()).handle(binding, req)
    assert result.outcome == "validation" and result.status == 422
    assert result.body["detail"] == "this stream is bound to alias Production"
    assert _Client.calls == []
    # ... and that result is a 422 problem document on this route
    assert _push_response_status(result) == 422


def _push_response_status(result):
    from examlops.dataplane.service.app import _push_response

    return _push_response(result, SimpleNamespace(model="JPCP")).status_code


def test_the_global_project_is_spelled_either_way(env):
    assert _push(env.client, project="_global").status_code == 200
    assert _push(env.client, project="proj").status_code == 200
    assert [b.project for b, _ in env.ingress.calls] == ["", "proj"]


# ── the auth matrix ─────────────────────────────────────────────────────────────────────────


def test_open_mode_refuses_ingest(make, monkeypatch):
    monkeypatch.delenv("DATAPLANE_TOKEN")
    monkeypatch.delenv("DATAPLANE_INGEST_TOKEN")
    _stream("", "push1")
    e = make()
    doc = _is_problem(_push(e.client, headers={}), 503, "unavailable")
    assert doc["detail"] == INGEST_DISABLED_DETAIL
    assert e.client.get("/streams").status_code == 200  # reads stay open on loopback
    assert e.ingress.calls == []


def test_the_ingest_token_can_push_and_nothing_else(env):
    assert _push(env.client, headers=HI).status_code == 200
    c = env.client
    body = {"connector": "one", "spec": {}}
    for r in (
        c.put("/sources/x", headers=HI, json=body),
        c.post("/sources/x/pull", headers=HI, json={}),
        c.delete("/sources/x", headers=HI),
        c.get("/sources", headers=HI),
        c.get("/streams", headers=HI),
        c.get("/streams/push1", headers=HI),
    ):
        assert r.status_code == 403, r.text
        assert "scope" in r.json()["detail"]


def test_an_ingest_token_alone_is_not_open_mode(make, monkeypatch):
    monkeypatch.delenv("DATAPLANE_TOKEN")
    _stream("", "push1")
    e = make()
    assert _push(e.client, headers=HI).status_code == 200
    # something is configured, so nothing is open — and nothing here can read or write
    assert e.client.get("/streams").status_code == 503
    assert e.client.put("/sources/x", headers=HI, json={"connector": "one"}).status_code == 503


@pytest.mark.parametrize("value", ["changeme", "tiny1"])
def test_a_placeholder_ingest_token_never_authenticates(make, monkeypatch, value):
    monkeypatch.setenv("DATAPLANE_INGEST_TOKEN", value)
    _stream("", "push1")
    e = make()
    bad = {"Authorization": f"Bearer {value}"}
    assert _push(e.client, headers=bad).status_code == 403  # the main token still works…
    assert _push(e.client, headers=H).status_code == 200
    monkeypatch.delenv("DATAPLANE_TOKEN")  # …and with nothing else configured: fail closed
    doc = _is_problem(_push(e.client, headers=bad), 503)
    assert "DATAPLANE_INGEST_TOKEN is set but is a placeholder or too short" in doc["detail"]
    assert value not in doc["detail"]


def test_missing_and_wrong_bearers_are_problem_documents(env):
    _is_problem(_push(env.client, headers={}), 401, "unauthorized")
    _is_problem(_push(env.client, headers={"Authorization": "Bearer nope-nope"}), 403, "forbidden")
    assert env.ingress.calls == []
    # M9: the WHOLE stream family speaks problem documents, not only the push route — one error
    # shape per resource family. `detail` is still there, so a caller reading that key is fine.
    r = env.client.get("/streams", headers={"Authorization": "Bearer nope-nope"})
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == PROBLEM_TYPE_BASE + "forbidden" and r.json()["detail"]
    # a route outside /streams still answers FastAPI's plain {"detail": …}
    other = env.client.get("/sources/nope", headers=H)
    assert other.status_code in (403, 404)
    assert other.headers["content-type"] == "application/json"


def _principal(role, projects=None):
    return iam_mod.Principal(
        provider="center",
        issuer="https://idp.example",
        subject="alice",
        tenant="t1",
        role=role,
        projects=projects or {},
    )


@pytest.fixture
def federated(env, monkeypatch):
    calls: list[dict] = []
    decision = {"allow": True}
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=True))

    def verify(token, config=None, *, provider_hint=None):
        role = token.split(".", 1)[0]
        return _principal(role)

    def authorize(principal, action, resource=None, context=None, **kw):
        calls.append({"action": action, "resource": resource, **kw})
        return iam_mod.Decision(decision["allow"], "ok" if decision["allow"] else "center says no")

    monkeypatch.setattr(iam_mod, "verify_access_token", verify)
    monkeypatch.setattr(iam_mod, "authorize", authorize)
    env.calls = calls
    env.decision = decision

    def grant(relation, project):
        from examlops import authz

        authz.grant("center:alice", relation, f"project:{project}")

    env.grant = grant
    return env


OP = {"Authorization": "Bearer operator.jwt.x"}
VIEW = {"Authorization": "Bearer viewer.jwt.x"}


def test_a_federated_operator_pushes_and_the_pdp_sees_dataplane_ingest(federated):
    assert _push(federated.client, headers=OP).status_code == 200
    call = federated.calls[-1]
    assert call["action"] == "dataplane.ingest" and call["local_allowed"] is True
    assert call["resource"] == {
        "type": "dataplane",
        "kind": "stream",
        "id": "_global/push1",
        "method": "POST",
        "project": "",
    }


def test_a_federated_viewer_gets_the_fixed_403(federated):
    exists = _push(federated.client, headers=VIEW)
    missing = _push(federated.client, "nope", headers=VIEW)
    for r in (exists, missing):
        assert _is_problem(r, 403, "forbidden")["detail"] == STREAM_FORBIDDEN_DETAIL
    assert exists.json() == missing.json()
    assert federated.ingress.calls == []


def test_the_center_pdp_can_veto_an_ingest(federated):
    federated.decision["allow"] = False
    doc = _is_problem(_push(federated.client, headers=OP), 403, "forbidden")
    assert "center says no" in doc["detail"]
    assert federated.ingress.calls == []


def test_a_project_editor_pushes_to_its_project_and_gets_the_same_403_elsewhere(
    federated, monkeypatch
):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("b", "push1")
    federated.grant("editor", "proj")
    c = federated.client
    assert _push(c, headers=VIEW, project="proj").status_code == 200  # editor, not operator
    other = _push(c, headers=VIEW, project="b")
    unknown = _push(c, "nope", headers=VIEW, project="b")
    assert other.status_code == unknown.status_code == 403
    assert other.json() == unknown.json()  # no existence oracle
    assert "b/push1" not in other.text
    # an operator without the relation is refused on a project stream under multitenancy …
    assert _push(c, headers=OP, project="b").status_code == 403
    # … and a project viewer is not an editor
    federated.grant("viewer", "b")
    assert _push(c, headers=VIEW, project="b").status_code == 403
    assert _push(c, headers=VIEW).status_code == 403  # a global stream needs operator


def test_multitenancy_off_never_turns_a_viewer_into_an_editor(federated):
    c = federated.client
    assert _push(c, headers=VIEW, project="proj").status_code == 403
    federated.grant("editor", "proj")  # an explicit grant still counts
    assert _push(c, headers=VIEW, project="proj").status_code == 200
    assert _push(c, headers=OP, project="proj").status_code == 200


def test_an_operator_project_claim_in_the_token_counts_as_editor(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    monkeypatch.setattr(
        iam_mod, "verify_access_token", lambda *a, **k: _principal("viewer", {"proj": "operator"})
    )
    assert _push(federated.client, headers=VIEW, project="proj").status_code == 200


# ── the body cap ────────────────────────────────────────────────────────────────────────────


def test_a_body_over_the_cap_is_413_by_content_length(make, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES", "64")
    _stream("", "push1")
    e = make()
    big = json.dumps({"payload": {"x": "y" * 100, "note": SECRET}})
    doc = _is_problem(_push(e.client, content=big), 413, "too-large")
    assert doc["outcome"] == "validation" and "64 bytes" in doc["detail"]
    assert e.ingress.calls == []
    assert _push(e.client, body={"x": 1}).status_code == 200


def test_a_chunked_body_cannot_bypass_the_cap(make, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES", "64")
    _stream("", "push1")
    e = make()
    sent: list[int] = []

    def chunks():
        for _ in range(10):
            sent.append(1)
            yield b'{"x": "' + b"y" * 20 + b'"}'

    r = e.client.post("/streams/push1/messages", content=chunks(), headers=H)
    _is_problem(r, 413, "too-large")
    assert e.ingress.calls == []

    def small():
        yield b'{"x": '
        yield b"1}"

    assert e.client.post("/streams/push1/messages", content=small(), headers=H).status_code == 200


def test_the_streamed_read_stops_at_the_cap(make, monkeypatch):
    """Driven over raw ASGI (the TestClient buffers a generator body into one message): the route
    reads a chunked body chunk by chunk and stops as soon as the running count passes the cap —
    it never buffers the rest of a body it is going to refuse."""
    import asyncio

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES", "64")
    _stream("", "push1")
    e = make()
    total, consumed = 50, 0

    async def receive():
        nonlocal consumed
        consumed += 1
        if consumed > total:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"x" * 16, "more_body": consumed < total}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    path = "/streams/push1/messages"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(e.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert consumed <= 5  # four 16-byte chunks fit in 64 bytes; the fifth crosses the cap
    assert e.ingress.calls == []


def test_a_streams_own_max_bytes_lowers_the_cap(make):
    _stream("", "tiny", limits={"max_bytes": 32})
    e = make()
    _is_problem(_push(e.client, "tiny", body={"x": "y" * 64}), 413, "too-large")
    assert _push(e.client, "tiny", body={"x": 1}).status_code == 200


def _requests_total(stream: str, outcome: str, project: str = "") -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "dataplane_stream_requests_total",
            {
                "project": project,
                "stream": stream,
                "connector": "http",
                "model": "JPCP",
                "outcome": outcome,
            },
        )
        or 0.0
    )


def test_a_refusal_the_route_makes_is_counted_like_any_other_request(make, monkeypatch):
    """Live finding D8: a 413 and an envelope rejection never reach the ingress, and nothing else
    counted them — so a stream refusing everything it was sent read, in ``requests_total``, as a
    stream nobody was using. Every refusal made once the stream is known is counted."""
    name = f"count-{uuid.uuid4().hex[:8]}"
    _stream("", name, limits={"max_bytes": 64})
    e = make()
    before = _requests_total(name, "validation")

    _is_problem(_push(e.client, name, body={"x": "y" * 200}), 413, "too-large")
    _is_problem(_push(e.client, name, content="this is not json"), 422, "validation")
    _is_problem(
        _push(e.client, name, headers={**H, "Idempotency-Key": "\x00bad"}), 422, "validation"
    )
    assert e.ingress.calls == []  # none of them reached the ingress
    assert _requests_total(name, "validation") == before + 3


def test_an_oversize_body_to_an_unknown_or_paused_stream_answers_for_the_stream(make):
    """Every size check waits for the binding, which is what makes the 413 countable. The answer
    for a stream that cannot serve the message at all is the stream's, at any size."""
    _stream("", "paused-big", state="paused")
    e = make()
    big = {"x": "y" * 4096}
    unknown = _is_problem(_push(e.client, "never-defined", body=big), 404, "not-found")
    paused = _is_problem(_push(e.client, "paused-big", body=big), 503, "paused")
    assert unknown["detail"] == "stream 'never-defined' not found"
    assert paused["detail"] == "stream paused"


def test_a_bad_cap_fails_at_startup(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES", "lots")
    with pytest.raises(ValueError, match="EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES"):
        create_app(start_scheduler=False, streams=True)


# ── headers ─────────────────────────────────────────────────────────────────────────────────


def test_idempotency_key_and_budget_headers(env):
    c = env.client
    for bad in ("k" * 201, "has space", "tab\tkey"):
        _is_problem(_push(c, headers={**H, "Idempotency-Key": bad}), 422, "validation")
    _is_problem(_push(c, headers={**H, "X-ExaMLOps-Budget-Ms": "soon"}), 422, "validation")
    assert env.ingress.calls == []
    for budget, expected in (("250", 250), ("999999999", 60_000), ("-5", 1), ("0", 1)):
        assert _push(c, headers={**H, "X-ExaMLOps-Budget-Ms": budget}).status_code == 200
        assert env.ingress.calls[-1][1].deadline_ms == expected
    assert _push(c, headers={**H, "Idempotency-Key": "k" * 200}).status_code == 200
    assert env.ingress.calls[-1][1].idempotency_key == "k" * 200
    assert _push(c, headers={**H, "traceparent": "garbage"}).status_code == 200
    assert env.ingress.calls[-1][1].traceparent is None  # malformed: ignored


def test_a_replay_answers_idempotent_replayed(env):
    env.ingress.result = IngressResult(
        outcome="ok", prediction=1.0, body={"model_version": "7"}, replayed=True
    )
    r = _push(env.client, headers={**H, "Idempotency-Key": "abc"})
    assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"
    env.ingress.result = IngressResult(outcome="model", status=500, replayed=True)
    r = _push(env.client, headers={**H, "Idempotency-Key": "abc"})
    assert r.status_code == 500 and r.headers["Idempotent-Replayed"] == "true"


# ── every status code ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "status", "slug", "retry_after"),
    [
        (IngressResult(outcome="validation", body={"detail": "missing field 'x'"}), 422, "validation", None),
        (IngressResult(outcome="not_found"), 404, "not-found", None),
        (IngressResult(outcome="overloaded", retry_after=1.0, shed_reason="in_flight"), 429, "shed", "1"),
        (IngressResult(outcome="overloaded", retry_after=0.25, shed_reason="rate"), 429, "shed", "1"),
        (IngressResult(outcome="overloaded", retry_after=12.2), 503, "overloaded", "13"),
        (IngressResult(outcome="overloaded"), 503, "overloaded", "5"),
        (IngressResult(outcome="deadline"), 504, "deadline", None),
        (IngressResult(outcome="transport"), 502, "transport", None),
        (IngressResult(outcome="model", body={"detail": f"bad input {SECRET}"}), 500, "model-failed", None),
        (IngressResult(outcome="unexpected"), 500, "unexpected", None),
    ],
)  # fmt: skip
def test_every_outcome_maps_to_its_status_and_problem(env, result, status, slug, retry_after):
    env.ingress.result = result
    doc = _is_problem(_push(env.client), status, slug)
    assert doc["outcome"] == result.outcome
    r = _push(env.client)
    assert r.headers.get("Retry-After") == retry_after
    if result.outcome == "validation":
        assert doc["detail"] == "missing field 'x'"


def test_a_non_finite_prediction_is_null_not_a_crash(env):
    env.ingress.result = IngressResult(outcome="ok", prediction=float("nan"), body={})
    r = _push(env.client)
    assert r.status_code == 200 and r.json()["prediction"] is None


# ── stream state ────────────────────────────────────────────────────────────────────────────


def test_paused_disabled_unknown_and_non_push_streams(env):
    c = env.client
    r = _push(c, "paused1")
    doc = _is_problem(r, 503, "paused")
    assert doc["detail"] == "stream paused" and r.headers["Retry-After"] == "30"
    _is_problem(_push(c, "off1"), 404, "not-found")
    _is_problem(_push(c, "never-defined"), 404, "not-found")
    _is_problem(_push(c, "kafka1"), 404, "not-push")
    assert env.ingress.calls == []


def test_a_state_change_reaches_the_push_route_through_the_catalog_view(env):
    assert _push(env.client).status_code == 200
    set_stream_state("", "push1", "paused")
    env.app.state.streams.catalog.refresh()  # what the supervisor's reconcile does every 10 s
    assert _push(env.client).status_code == 503


# ── the read routes ─────────────────────────────────────────────────────────────────────────


def test_the_read_routes_show_runtime_and_stats_but_never_option_values(make):
    _stream("proj", "push1", options={"api_secret": "opt-value-hunter2", "passthrough": ["a"]})
    e = make()
    assert _push(e.client, project="proj").status_code == 200  # builds this process's stack
    listed = e.client.get("/streams", headers=H).json()
    one = e.client.get("/streams/push1", headers=H, params={"project": "proj"}).json()
    assert [s["name"] for s in listed] == ["push1"] and listed[0] == one
    assert one["option_keys"] == ["api_secret", "passthrough"]
    assert one["runtime"]["state"] == "running" and one["stats"]["requests"] == 3
    assert "opt-value-hunter2" not in json.dumps(listed)
    assert e.client.get("/streams/nope", headers=H).status_code == 404


def test_the_read_routes_are_project_filtered(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("b", "push1")
    federated.grant("viewer", "proj")
    c = federated.client
    listed = {(s["project"], s["name"]) for s in c.get("/streams", headers=VIEW).json()}
    assert ("proj", "push1") in listed and ("b", "push1") not in listed
    assert c.get("/streams", headers=VIEW, params={"project": "b"}).status_code == 403
    denied = c.get("/streams/push1", headers=VIEW, params={"project": "b"})
    missing = c.get("/streams/nope", headers=VIEW, params={"project": "b"})
    assert denied.status_code == missing.status_code == 403 and denied.json() == missing.json()
    assert c.get("/streams/push1", headers=VIEW, params={"project": "proj"}).status_code == 200


def test_every_stream_route_is_authenticated_and_project_authorised(env):
    stream_routes = [
        r for r in env.app.routes if isinstance(r, APIRoute) and r.path.startswith("/streams")
    ]
    assert {(r.path, tuple(sorted(r.methods))) for r in stream_routes} == {
        ("/streams", ("GET",)),
        ("/streams/{name}", ("GET",)),
        ("/streams/{name}/messages", ("POST",)),
        ("/streams/{name}/state", ("POST",)),  # A8b
        ("/streams/{name}/dead-letters", ("GET",)),
        ("/streams/{name}/dead-letters", ("DELETE",)),
        ("/streams/{name}/dead-letters/{dead_letter_id}", ("GET",)),
        ("/streams/{name}/dead-letters/{dead_letter_id}/replay", ("POST",)),
    }
    # A8b write routes: the state change, the purge and the replay.
    write_routes = {"/streams/{name}/state", "/streams/{name}/dead-letters/{dead_letter_id}/replay"}
    for route in stream_routes:
        deps = {d.call for d in route.dependant.dependencies}
        if route.path.endswith("/messages"):
            assert deps == {authenticate_ingest}
        elif route.path == "/streams":
            assert deps == {require_read}
        elif route.path in write_routes or (
            route.path == "/streams/{name}/dead-letters" and "DELETE" in route.methods
        ):
            assert deps == {authenticate_write}
        else:
            assert deps == {authenticate_read}
        if deps & {authenticate_ingest, authenticate_read, authenticate_write}:
            # called directly, or handed to the threadpool (`run_in_threadpool(authorize_stream, …)`)
            assert "authorize_stream" in inspect.getsource(route.endpoint)


# ── roles ───────────────────────────────────────────────────────────────────────────────────


def _paths(app) -> set[str]:
    return {r.path for r in app.routes if isinstance(r, APIRoute)}


def test_role_all_mounts_everything_and_runs_the_supervisor(make):
    e = make()
    assert {"/streams/{name}/messages", "/sources", "/health"} <= _paths(e.app)
    assert e.supervisor is not None and e.supervisor.started


def test_role_api_mounts_the_routes_but_runs_no_supervisor(make, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ROLE", "api")
    e = make()
    assert {"/streams/{name}/messages", "/streams", "/sources"} <= _paths(e.app)
    assert e.supervisor is None
    assert e.client.get("/health").json()["role"] == "api"


def test_role_streams_runs_the_supervisor_and_serves_only_health(make, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ROLE", "streams")
    e = make()
    assert e.supervisor is not None and e.supervisor.started
    assert _paths(e.app) == {"/health", "/ready", "/metrics"}
    assert e.client.get("/health").status_code == 200
    assert e.client.get("/ready").json() == {"status": "alive"}
    assert e.client.get("/metrics").status_code == 200
    assert (
        _push(e.client).status_code == 404
        and e.client.get("/sources", headers=H).status_code == 404
    )


def test_an_invalid_role_fails_at_startup(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ROLE", "everything")
    with pytest.raises(ValueError, match="EXAMLOPS_DATAPLANE_ROLE"):
        create_app(start_scheduler=False)


def test_streams_default_on_whatever_start_scheduler_says():
    """R21(1): the role alone decides; `start_scheduler` governs only the pull scheduler."""
    assert "/streams/{name}/messages" in _paths(create_app(start_scheduler=False))
    off = create_app(start_scheduler=False, streams=False)
    assert not any(p.startswith("/streams") for p in _paths(off))


# ── drain ───────────────────────────────────────────────────────────────────────────────────


def test_the_drain_runs_in_order(make, caplog):
    e = make()
    e.client.post("/streams/push1/messages", json={"x": 1}, headers=H)  # builds the stack
    e.app.state.scheduler.stop = lambda wait_s=10.0: e.log.append("scheduler.stop")
    with caplog.at_level(logging.INFO, logger="examlops.dataplane.service.app"):
        e.client.__exit__(None, None, None)
    # Live finding D8: the whole drain was silent, so a log could not say whether a process
    # drained or was killed. One line when it begins and one when it ends.
    assert "drain started" in caplog.text and "drain finished" in caplog.text
    assert e.log == [
        "supervisor.stop draining=True",  # 1 (draining on) then 2
        "drift",  # 4
        "spool",
        "client",
        "supervisor.release_leases",  # 5
        "scheduler.stop",  # 6
    ]


def test_while_draining_ready_and_push_answer_503(env):
    env.app.state.streams.draining.set()
    r = env.client.get("/ready")
    assert r.status_code == 503 and r.json() == {"status": "draining"}
    r = _push(env.client)
    doc = _is_problem(r, 503, "draining")
    assert r.headers["Retry-After"] == "5" and "draining" in doc["detail"]
    assert env.ingress.calls == []


def _runtime(drain_s: float, log: list[str]):
    rt = app_mod._StreamRuntime(
        stack_factory=lambda: IngressStack(
            object(), spool=Part("spool", log), drift=Part("drift", log), client=Part("client", log)
        ),
        catalog_view=None,
        push_max_bytes=1024,
        drain_s=drain_s,
    )
    rt.stack()
    rt.supervisor = FakeSupervisor(log, {"rt": rt})
    return rt


def test_the_drain_waits_for_in_flight_pushes_between_intake_and_flush():
    log: list[str] = []
    rt = _runtime(5.0, log)
    scheduler = SimpleNamespace(stop=lambda wait_s: log.append("scheduler.stop"))
    rt.inflight.enter()

    def finish():
        time.sleep(0.2)
        log.append("push finished")
        rt.inflight.exit()

    threading.Thread(target=finish).start()
    app_mod._drain(rt, scheduler)
    assert log == [
        "supervisor.stop draining=True",
        "push finished",
        "drift",
        "spool",
        "client",
        "supervisor.release_leases",
        "scheduler.stop",
    ]


def test_the_drain_is_bounded_by_the_drain_seconds():
    log: list[str] = []
    rt = _runtime(0.3, log)
    waits: list[float] = []
    scheduler = SimpleNamespace(stop=lambda wait_s: waits.append(wait_s))
    rt.inflight.enter()  # a push that never finishes
    started = time.monotonic()
    app_mod._drain(rt, scheduler)
    assert time.monotonic() - started < 2.0
    assert rt.supervisor.stop_timeout is not None and rt.supervisor.stop_timeout <= 0.3
    assert waits and waits[0] <= 0.3
    assert log[-2:] == ["client", "supervisor.release_leases"]  # every step still ran


def test_a_failing_drain_step_never_skips_the_rest():
    log: list[str] = []
    rt = _runtime(1.0, log)

    def boom(timeout):
        log.append("supervisor.stop")
        raise RuntimeError("stuck")

    rt.supervisor.stop = boom
    app_mod._drain(rt, SimpleNamespace(stop=lambda wait_s: log.append("scheduler.stop")))
    assert log == [
        "supervisor.stop",
        "drift",
        "spool",
        "client",
        "supervisor.release_leases",
        "scheduler.stop",
    ]


def test_sigterm_turns_draining_on_and_chains_to_the_previous_handler():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be installed from the main thread")
    got: list[int] = []
    original = signal.signal(signal.SIGTERM, lambda signum, frame: got.append(signum))
    try:
        rt = _runtime(1.0, [])
        undo = app_mod._install_sigterm(rt)
        signal.raise_signal(signal.SIGTERM)
        assert rt.draining.is_set() and got == [signal.SIGTERM]
        undo()
        signal.raise_signal(signal.SIGTERM)
        assert got == [signal.SIGTERM, signal.SIGTERM]  # the previous handler is back
    finally:
        signal.signal(signal.SIGTERM, original)


# ── the real wiring ─────────────────────────────────────────────────────────────────────────


def test_the_real_stack_and_supervisor_wire_up_and_drain(monkeypatch):
    """No fakes: `IngressStack.build` + `StreamSupervisor` behind the real routes. The inference
    service is a closed local port, so the push is a `transport` outcome (502) end to end."""
    from examlops.dataplane.streams.supervisor import StreamSupervisor

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_INFERENCE_URL", "http://127.0.0.1:9")
    _stream("", "push1", model="NoSchemaModel")
    app = create_app(start_scheduler=False, streams=True)
    with TestClient(app) as c:
        rt = app.state.streams
        assert isinstance(rt.supervisor, StreamSupervisor)
        doc = _is_problem(_push(c, body={"x": 1}), 502, "transport")
        assert doc["outcome"] == "transport"
        view = c.get("/streams/push1", headers=H).json()
        assert view["runtime"] is None  # push streams are served by the route, not supervised
        assert view["stats"]["outcomes"] == {"transport": 1}
        stack = rt.built_stack()
    assert rt.draining.is_set() and stack.client.closed  # the lifespan exit drained it


# ── fix round 1 ─────────────────────────────────────────────────────────────────────────────


def _asgi(app, receive, *, headers=None, path="/streams/push1/messages"):
    """Drive one push over raw ASGI; returns (status or None, messages sent)."""
    import asyncio

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    base = [(b"host", b"testserver"), (b"authorization", f"Bearer {TOKEN}".encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": base + list(headers or []),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    return (start["status"] if start else None), sent


def _body(sent) -> dict:
    return json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )


def test_a_body_that_does_not_arrive_in_time_is_408(make, monkeypatch):
    """M1: a stalled sender cannot hold a connection and an in-flight slot open."""
    import asyncio

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_READ_TIMEOUT", "0.2")
    _stream("", "push1")
    e = make()
    calls = [0]

    async def receive():
        calls[0] += 1
        if calls[0] == 1:
            return {"type": "http.request", "body": b'{"x": ', "more_body": True}
        await asyncio.sleep(5)  # the rest never comes
        return {"type": "http.request", "body": b"1}", "more_body": False}

    started = time.monotonic()
    status, sent = _asgi(e.app, receive)
    assert status == 408 and time.monotonic() - started < 3
    doc = _body(sent)
    assert doc["type"] == PROBLEM_TYPE_BASE + "timeout" and doc["status"] == 408
    assert e.ingress.calls == [] and e.app.state.streams.inflight.count == 0


def test_a_bad_read_timeout_fails_at_startup(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_PUSH_READ_TIMEOUT", "0")
    with pytest.raises(ValueError, match="EXAMLOPS_DATAPLANE_PUSH_READ_TIMEOUT"):
        create_app(start_scheduler=False)


@pytest.mark.parametrize("length", ["-5", "+12", "1_000", "12abc", ""])
def test_a_content_length_that_is_not_digits_is_400(make, length):
    """M2: `int()` accepts a sign and underscores; the route does not."""
    _stream("", "push1")
    e = make()

    async def receive():
        return {"type": "http.request", "body": b'{"x": 1}', "more_body": False}

    status, sent = _asgi(e.app, receive, headers=[(b"content-length", length.encode())])
    assert status == 400 and _body(sent)["type"] == PROBLEM_TYPE_BASE + "bad-request"
    assert e.ingress.calls == []


def test_a_client_that_leaves_mid_body_is_let_go_quietly(make, caplog):
    """M3: a disconnect is no error: no traceback, and the in-flight slot is given back."""
    _stream("", "push1")
    e = make()
    calls = [0]

    async def receive():
        calls[0] += 1
        if calls[0] == 1:
            return {"type": "http.request", "body": b'{"x": ', "more_body": True}
        return {"type": "http.disconnect"}

    with caplog.at_level("DEBUG"):
        _asgi(e.app, receive)  # does not raise
    assert "Traceback" not in caplog.text and "ClientDisconnect" not in caplog.text
    assert e.ingress.calls == [] and e.app.state.streams.inflight.count == 0


def test_a_failed_stack_build_is_not_retried_for_five_seconds(monkeypatch):
    """M10: a broken dependency is not rebuilt (and torn down) on every request."""
    builds: list[int] = []

    def broken():
        builds.append(1)
        raise OSError("platform.db unavailable")

    _stream("", "push1")
    app = create_app(
        start_scheduler=False,
        streams=True,
        stack_factory=broken,
        supervisor_factory=lambda ingress, view: FakeSupervisor([], {}),
    )
    with TestClient(app) as c:  # role `all`: the supervisor start already tried once
        first = len(builds)
        for _ in range(3):
            _is_problem(_push(c), 503, "unavailable")
        assert len(builds) == first  # remembered, not retried
        rt = app.state.streams
        rt._stack_failed_at -= 5.0  # five seconds later
        _is_problem(_push(c), 503, "unavailable")
        assert len(builds) == first + 1


def test_one_deadline_bounds_the_whole_shutdown():
    """I2: SIGTERM fixes THE deadline; the server's graceful wait and the lifespan drain share it.
    Every bounded step here runs to its full timeout (an injected clock), and the whole shutdown
    still ends within the drain seconds plus the flush floors (≤ 2 s)."""
    now = [100.0]

    def advance(seconds: float | None) -> None:
        now[0] += max(0.0, seconds or 0.0)

    class Slow:
        def close(self, timeout: float | None = None) -> None:
            advance(timeout)

    class Sup:
        def stop(self, timeout: float) -> None:
            advance(timeout)

        def release_leases(self, timeout: float | None = None) -> None:
            advance(timeout)

    class StuckInFlight:
        count = 1

        def wait_idle(self, timeout: float) -> bool:
            advance(timeout)
            return False

    for server_wait in (0.0, 15.0, 20.0):
        rt = app_mod._StreamRuntime(
            stack_factory=lambda: IngressStack(object(), spool=Slow(), drift=Slow(), client=Slow()),
            catalog_view=None,
            push_max_bytes=1024,
            drain_s=20.0,
            clock=lambda: now[0],
        )
        rt.stack()
        rt.supervisor = Sup()
        rt.inflight = StuckInFlight()
        t0 = now[0]
        rt.begin_drain()  # SIGTERM
        advance(server_wait)  # uvicorn waits for in-flight HTTP (its timeout: the same budget)
        app_mod._drain(rt, SimpleNamespace(stop=lambda wait_s: advance(wait_s)))
        assert now[0] - t0 <= 20.0 + 2.0, (server_wait, now[0] - t0)
        assert rt.begin_drain() == t0 + 20.0  # the deadline never moved


def test_sigterm_stops_intake_at_once_and_the_lifespan_drain_does_not_repeat_it():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be installed from the main thread")
    log: list[str] = []
    rt = _runtime(5.0, log)
    original = signal.signal(signal.SIGTERM, lambda signum, frame: None)
    try:
        undo = app_mod._install_sigterm(rt)
        signal.raise_signal(signal.SIGTERM)
        assert rt.draining.is_set()
        deadline = time.monotonic() + 5
        while "supervisor.stop draining=True" not in log and time.monotonic() < deadline:
            time.sleep(0.01)
        assert log == ["supervisor.stop draining=True"]  # intake stopped before the lifespan exit
        app_mod._drain(rt, SimpleNamespace(stop=lambda wait_s: log.append("scheduler.stop")))
        assert log == [
            "supervisor.stop draining=True",  # once
            "drift",
            "spool",
            "client",
            "supervisor.release_leases",
            "scheduler.stop",
        ]
        undo()
    finally:
        signal.signal(signal.SIGTERM, original)


def test_the_entrypoint_gives_uvicorn_the_drain_budget():
    """R21(3): the server's graceful-shutdown wait is the same budget as the drain."""
    import ast
    from pathlib import Path

    main_py = Path(__file__).parents[2] / "platform" / "services" / "dataplane" / "main.py"
    run = next(
        n
        for n in ast.walk(ast.parse(main_py.read_text()))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run"
    )
    kw = {k.arg: ast.unparse(k.value) for k in run.keywords}
    assert kw["timeout_graceful_shutdown"] == "int(drain_seconds())"
