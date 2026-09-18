"""Dataplane runtime control + dead letters (ADR 0131 d7, dataplane Plan 2 task A8b).

``POST /streams/{name}/state`` (enable/pause/resume/disable), the four dead-letter routes and the
supervisor's pause/resume-in-place policy that backs them. The ingress and supervisor are fakes at
the service level (this file tests the service, not the ingress or the real supervisor threading —
``test_dataplane_streams_supervisor.py`` covers that), except for the "pause semantics per
connector kind" section, which drives a real :class:`StreamSupervisor` with fake connectors.

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examlops import iam as iam_mod
from examlops.coordination import DbCoordinator
from examlops.data import dataplane as catalog
from examlops.data.audit import export_audit_events
from examlops.data.dataplane import get_stream, set_stream_state, upsert_stream
from examlops.dataplane.service.app import create_app
from examlops.dataplane.service.auth import STREAM_FORBIDDEN_DETAIL
from examlops.dataplane.streams.dlq import (
    DbDeadLetterSink,
    claim_lost_result,
)
from examlops.dataplane.streams.supervisor import IngressStack, StreamSupervisor
from examlops.dataplane.streams.types import InferenceResult, StreamBinding, StreamLimits
from examlops.dataplane.types import SpecError

TOKEN = "a-real-dataplane-token-0123456789"
INGEST = "an-ingest-only-token-9876543210"
H = {"Authorization": f"Bearer {TOKEN}"}
HI = {"Authorization": f"Bearer {INGEST}"}
OK = InferenceResult(outcome="ok", prediction=0.5, body={"prediction": 0.5, "model_version": "3"})


# ── fakes (service level) ───────────────────────────────────────────────────────────────────


class FakeIngress:
    def __init__(self) -> None:
        self.result: Any = OK
        self.calls: list[tuple[Any, Any]] = []

    def handle(self, binding, req, *, reply=None):
        self.calls.append((binding, req))
        if reply is not None:
            reply(self.result)
        return self.result

    def stats(self):
        return {}


class FakeSupervisor:
    def __init__(self) -> None:
        self.started = False
        self.paused: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, str]] = []

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float) -> None:
        pass

    def release_leases(self, timeout: float | None = None) -> None:
        pass

    def status_of(self, project, name):
        return None

    def pause_stream(self, project: str, name: str) -> None:
        self.paused.append((project, name))

    def resume_stream(self, project: str, name: str) -> None:
        self.resumed.append((project, name))


class Part:
    def close(self, timeout: float | None = None) -> None:
        pass


@pytest.fixture(autouse=True)
def _iam_off(monkeypatch, tmp_path):
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=False))
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    for var in ("EXAMLOPS_DATAPLANE_ROLE", "EXAMLOPS_MULTITENANCY"):
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
    supervisor: FakeSupervisor
    app: Any


@pytest.fixture
def make():
    opened: list[TestClient] = []

    def _make(**kw: Any) -> Env:
        ingress = FakeIngress()
        made: dict[str, Any] = {}

        def stack_factory():
            return IngressStack(ingress, spool=Part(), drift=Part(), client=Part())

        def supervisor_factory(ing, view):
            made["sup"] = FakeSupervisor()
            return made["sup"]

        app = create_app(
            start_scheduler=False,
            streams=True,
            stack_factory=stack_factory,
            supervisor_factory=supervisor_factory,
            **kw,
        )
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return Env(client=client, ingress=ingress, supervisor=made.get("sup"), app=app)

    yield _make
    for c in opened:
        try:
            c.__exit__(None, None, None)
        except Exception:
            pass


@pytest.fixture
def env(make):
    _stream("", "s1")
    return make()


def _seed_dl(
    project: str,
    stream: str,
    *,
    reason: str = "invalid_message",
    error: str = "boom",
    payload: str | None = None,
    origin: dict[str, Any] | None = None,
) -> int:
    o = origin or {}
    dl_id, _created = catalog.upsert_dead_letter(
        project,
        stream,
        reason=reason,
        error=error,
        attempts=1,
        sha256=None,
        size=len(payload) if payload else 0,
        origin=o,
        origin_topic=o.get("topic"),
        origin_partition=o.get("partition"),
        origin_offset=o.get("offset"),
        payload=payload,
        payload_encoding="utf-8" if payload is not None else None,
        payload_truncated=False,
    )
    return dl_id


def _audit(action: str) -> list[dict[str, Any]]:
    return [e for e in export_audit_events() if e["action"] == action]


def _details(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("details") or event.get("details_json") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


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


# ── POST /streams/{name}/state ──────────────────────────────────────────────────────────────


def test_the_state_body_accepts_exactly_the_catalogs_states():
    """M4: ``StreamStateBody.state`` is a pydantic ``Literal`` and cannot be built from the
    ``STREAM_STATES`` tuple, so the two are written twice. This is what keeps them in step —
    a state added to the catalog and not to the route would be a silent 422."""
    from examlops.data.dataplane import STREAM_STATES
    from examlops.dataplane.service.app import StreamStateBody

    declared = set(StreamStateBody.model_fields["state"].annotation.__args__)
    assert declared == set(STREAM_STATES)


def test_state_persists_and_a_second_replica_sees_it_on_reconcile(env):
    r = env.client.post("/streams/s1/state", headers=H, json={"state": "paused"})
    assert r.status_code == 200, r.text
    assert r.json() == {"project": "", "name": "s1", "state": "paused", "changed": True}
    # a second replica reads the persisted row directly, independent of this process's caches
    assert get_stream("s1", "")["state"] == "paused"


@pytest.mark.parametrize(
    ("start", "target", "event"),
    [
        ("enabled", "paused", "dataplane_stream_paused"),
        ("paused", "enabled", "dataplane_stream_resumed"),
        ("enabled", "disabled", "dataplane_stream_disabled"),
        ("disabled", "enabled", "dataplane_stream_enabled"),
    ],
)
def test_state_transitions_are_audited_with_the_right_event_and_previous_state(
    env, start, target, event
):
    set_stream_state("", "s1", start)
    r = env.client.post("/streams/s1/state", headers=H, json={"state": target, "reason": "ops"})
    assert r.status_code == 200 and r.json()["changed"] is True
    rows = _audit(event)
    assert len(rows) == 1
    details = _details(rows[0])
    assert details == {
        "project": "",
        "name": "s1",
        "previous_state": start,
        "state": target,
        "reason": "ops",
    }
    assert rows[0]["actor"] is not None


def test_a_no_op_state_change_reports_changed_false_and_writes_no_audit(env):
    r = env.client.post("/streams/s1/state", headers=H, json={"state": "enabled"})
    assert r.status_code == 200
    assert r.json() == {"project": "", "name": "s1", "state": "enabled", "changed": False}
    assert _audit("dataplane_stream_enabled") == []
    assert _audit("dataplane_stream_resumed") == []


@pytest.mark.parametrize("reason", ["removed_from_pack", "removed_from_pack:paused"])
def test_a_sweep_reserved_reason_is_refused(env, reason):
    r = env.client.post("/streams/s1/state", headers=H, json={"state": "paused", "reason": reason})
    assert r.status_code == 422, r.text
    assert reason in r.json()["detail"]
    assert get_stream("s1", "")["state"] == "enabled"  # untouched


def test_the_routes_reserved_reasons_stay_in_sync_with_what_the_sweep_actually_writes(env):
    """Guard (review round 1): the route must refuse every reason
    ``examlops.dataplane.streams.bindings``'s sweep would itself write — sourced from the sweep's
    own function, not a hand-copied literal — so a future change to the sweep's reason format
    cannot silently stop being reserved here."""
    from examlops.dataplane.streams.bindings import _sweep_disable_reason, is_sweep_reason

    for prior in ("enabled", "paused", "disabled"):
        reason = _sweep_disable_reason(prior)
        assert is_sweep_reason(reason)
        r = env.client.post(
            "/streams/s1/state", headers=H, json={"state": "paused", "reason": reason}
        )
        assert r.status_code == 422, (reason, r.text)
    assert not is_sweep_reason("ops") and not is_sweep_reason(None)
    assert (
        env.client.post(
            "/streams/s1/state", headers=H, json={"state": "paused", "reason": "ops"}
        ).status_code
        == 200
    )


def test_a_reason_over_200_characters_is_a_422(env):
    r = env.client.post(
        "/streams/s1/state", headers=H, json={"state": "paused", "reason": "x" * 201}
    )
    assert r.status_code == 422


def test_state_route_calls_the_supervisors_pause_and_resume_at_once(env):
    env.client.post("/streams/s1/state", headers=H, json={"state": "paused"})
    assert env.supervisor.paused == [("", "s1")]
    env.client.post("/streams/s1/state", headers=H, json={"state": "enabled"})
    assert env.supervisor.resumed == [("", "s1")]


def test_state_route_never_touches_origin(env):
    before = get_stream("s1", "")["origin"]
    env.client.post("/streams/s1/state", headers=H, json={"state": "disabled"})
    assert get_stream("s1", "")["origin"] == before == "api"


def test_state_route_unknown_and_other_project_streams(env):
    assert (
        env.client.post("/streams/nope/state", headers=H, json={"state": "paused"}).status_code
        == 404
    )
    assert (
        env.client.post(
            "/streams/s1/state", headers=H, params={"project": "other"}, json={"state": "paused"}
        ).status_code
        == 404
    )


def test_state_route_rejects_an_unknown_state_value(env):
    r = env.client.post("/streams/s1/state", headers=H, json={"state": "bogus"})
    assert r.status_code == 422


def test_state_route_auth_matrix(env):
    body = {"state": "paused"}
    assert env.client.post("/streams/s1/state", headers={}, json=body).status_code == 401
    r = env.client.post("/streams/s1/state", headers=HI, json=body)
    assert r.status_code == 403 and "scope" in r.json()["detail"]
    assert env.client.post("/streams/s1/state", headers=H, json=body).status_code == 200


def test_state_route_federated_auth_matrix(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    _stream("other", "s1")
    c = federated.client
    body = {"state": "paused"}
    # a plain viewer never even holds the raw `write` scope (federated non-operator callers only
    # ever get read+ingest) — refused before any project check is reached, on every project
    assert (
        c.post("/streams/s1/state", headers=VIEW, params={"project": "proj"}, json=body).status_code
        == 403
    )
    # an operator token with no relation on the project: the fixed 403, identical for a real and
    # an unknown stream (no existence oracle) — exactly A8's own pattern for other writes
    denied = c.post("/streams/s1/state", headers=OP, params={"project": "proj"}, json=body)
    missing = c.post("/streams/nope/state", headers=OP, params={"project": "proj"}, json=body)
    assert denied.status_code == missing.status_code == 403
    assert denied.json()["detail"] == missing.json()["detail"] == STREAM_FORBIDDEN_DETAIL
    assert denied.json() == missing.json()
    # another project: the identical fixed 403
    other = c.post("/streams/s1/state", headers=OP, params={"project": "other"}, json=body)
    assert other.status_code == 403 and other.json() == denied.json()
    # an editor grant on `proj` lets the (operator-scoped) write through
    federated.grant("editor", "proj")
    assert (
        c.post("/streams/s1/state", headers=OP, params={"project": "proj"}, json=body).status_code
        == 200
    )
    # the center PDP can still veto
    federated.decision["allow"] = False
    vetoed = c.post("/streams/s1/state", headers=OP, params={"project": "proj"}, json=body)
    assert vetoed.status_code == 403 and "center says no" in vetoed.json()["detail"]


# ── GET /streams/{name}/dead-letters ────────────────────────────────────────────────────────


def test_list_dead_letters_hides_payload_and_reports_has_payload(env):
    _seed_dl("", "s1", payload='{"x": 1}')
    _seed_dl("", "s1", payload=None)
    r = env.client.get("/streams/s1/dead-letters", headers=H)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert all("payload" not in i for i in items)
    assert {i["has_payload"] for i in items} == {True, False}


def test_list_dead_letters_filters_by_reason(env):
    _seed_dl("", "s1", reason="oversize")
    _seed_dl("", "s1", reason="not_json")
    items = env.client.get(
        "/streams/s1/dead-letters", headers=H, params={"reason": "oversize"}
    ).json()["items"]
    assert [i["reason"] for i in items] == ["oversize"]


def test_list_dead_letters_pagination_defaults_and_is_capped(env):
    for i in range(5):
        _seed_dl("", "s1", error=f"e{i}")
    default = env.client.get("/streams/s1/dead-letters", headers=H).json()
    assert len(default["items"]) == 5  # fewer than the default 50
    capped = env.client.get("/streams/s1/dead-letters", headers=H, params={"limit": 100000}).json()
    assert len(capped["items"]) == 5  # still just the 5 that exist; the cap never errors
    small = env.client.get("/streams/s1/dead-letters", headers=H, params={"limit": 2}).json()
    assert len(small["items"]) == 2 and small["next_cursor"] is not None
    nxt = env.client.get(
        "/streams/s1/dead-letters", headers=H, params={"limit": 2, "cursor": small["next_cursor"]}
    ).json()
    assert len(nxt["items"]) == 2
    seen_ids = {i["id"] for i in small["items"]} | {i["id"] for i in nxt["items"]}
    assert len(seen_ids) == 4  # no overlap between the two pages


def test_list_dead_letters_unknown_and_other_project_stream_is_404(env):
    assert env.client.get("/streams/nope/dead-letters", headers=H).status_code == 404
    assert (
        env.client.get(
            "/streams/s1/dead-letters", headers=H, params={"project": "other"}
        ).status_code
        == 404
    )


def test_list_dead_letters_auth_matrix(env):
    assert env.client.get("/streams/s1/dead-letters", headers={}).status_code == 401
    r = env.client.get("/streams/s1/dead-letters", headers=HI)
    assert r.status_code == 403 and "scope" in r.json()["detail"]
    assert env.client.get("/streams/s1/dead-letters", headers=H).status_code == 200


def test_list_dead_letters_federated_auth_matrix(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    _stream("other", "s1")
    c = federated.client
    denied = c.get("/streams/s1/dead-letters", headers=VIEW, params={"project": "proj"})
    missing = c.get("/streams/nope/dead-letters", headers=VIEW, params={"project": "proj"})
    assert denied.status_code == missing.status_code == 403
    assert denied.json() == missing.json()
    other = c.get("/streams/s1/dead-letters", headers=VIEW, params={"project": "other"})
    assert other.status_code == 403 and other.json() == denied.json()
    federated.grant("viewer", "proj")
    assert (
        c.get("/streams/s1/dead-letters", headers=VIEW, params={"project": "proj"}).status_code
        == 200
    )
    federated.decision["allow"] = False
    vetoed = c.get("/streams/s1/dead-letters", headers=VIEW, params={"project": "proj"})
    assert vetoed.status_code == 403 and "center says no" in vetoed.json()["detail"]


# ── GET /streams/{name}/dead-letters/{id} ───────────────────────────────────────────────────


def test_get_dead_letter_hides_the_payload_by_default(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    r = env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers=H)
    assert r.status_code == 200
    assert "payload" not in r.json() and r.json()["has_payload"] is True
    assert _audit("dataplane_stream_dlq_payload_read") == []


def test_get_dead_letter_shows_the_payload_only_with_write_and_include_payload(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    # write scope but include_payload not set: still hidden
    r = env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers=H)
    assert "payload" not in r.json()
    # include_payload set but no write scope (an unauthenticated open read has no scopes at all)
    r2 = env.client.get(
        f"/streams/s1/dead-letters/{dl_id}", headers={}, params={"include_payload": "true"}
    )
    assert r2.status_code == 401
    r3 = env.client.get(
        f"/streams/s1/dead-letters/{dl_id}", headers=H, params={"include_payload": "true"}
    )
    assert r3.status_code == 200
    assert r3.json()["payload"] == '{"x": 1}'
    rows = _audit("dataplane_stream_dlq_payload_read")
    assert len(rows) == 1 and _details(rows[0]) == {"id": dl_id, "project": "", "stream": "s1"}


def test_get_dead_letter_unknown_id_or_wrong_stream_is_404(env):
    _stream("", "s2")
    dl_id = _seed_dl("", "s2")
    assert env.client.get("/streams/s1/dead-letters/999999", headers=H).status_code == 404
    assert env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers=H).status_code == 404


def test_get_dead_letter_auth_matrix(env):
    dl_id = _seed_dl("", "s1")
    assert env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers={}).status_code == 401
    r = env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers=HI)
    assert r.status_code == 403 and "scope" in r.json()["detail"]
    assert env.client.get(f"/streams/s1/dead-letters/{dl_id}", headers=H).status_code == 200


def test_get_dead_letter_federated_auth_matrix(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    _stream("other", "s1")
    dl_id = _seed_dl("proj", "s1")
    c = federated.client
    denied = c.get(f"/streams/s1/dead-letters/{dl_id}", headers=VIEW, params={"project": "proj"})
    missing = c.get("/streams/nope/dead-letters/1", headers=VIEW, params={"project": "proj"})
    assert denied.status_code == missing.status_code == 403
    assert denied.json() == missing.json()
    other = c.get(f"/streams/s1/dead-letters/{dl_id}", headers=VIEW, params={"project": "other"})
    assert other.status_code == 403 and other.json() == denied.json()
    federated.grant("viewer", "proj")
    assert (
        c.get(
            f"/streams/s1/dead-letters/{dl_id}", headers=VIEW, params={"project": "proj"}
        ).status_code
        == 200
    )
    federated.decision["allow"] = False
    vetoed = c.get(f"/streams/s1/dead-letters/{dl_id}", headers=VIEW, params={"project": "proj"})
    assert vetoed.status_code == 403 and "center says no" in vetoed.json()["detail"]


def test_get_dead_letter_federated_operator_without_a_write_relation_never_sees_payload(
    federated, monkeypatch
):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    dl_id = _seed_dl("proj", "s1", payload='{"x": 1}')
    federated.grant("viewer", "proj")
    c = federated.client
    r = c.get(
        f"/streams/s1/dead-letters/{dl_id}",
        headers=VIEW,
        params={"project": "proj", "include_payload": "true"},
    )
    assert r.status_code == 200 and "payload" not in r.json()  # a viewer never gets the write scope


# ── POST /streams/{name}/dead-letters/{id}/replay ───────────────────────────────────────────


def test_replay_returns_the_ingress_outcome(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "ok" and body["replayed"] is True and body["prediction"] == 0.5
    assert len(env.ingress.calls) == 1
    assert get_stream("s1", "")  # sanity: the stream itself is untouched


def test_replay_maps_a_lost_claim_to_409(env, monkeypatch):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    monkeypatch.setattr(
        "examlops.dataplane.streams.dlq.replay", lambda *a, **k: claim_lost_result(dl_id)
    )
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert r.status_code == 409, r.text
    assert r.json()["outcome"] == "unexpected" and r.json()["replayed"] is False
    assert "another replay took over" in r.json()["detail"]


def test_replay_a_double_replay_without_force_is_409(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    first = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert first.status_code == 200
    second = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert second.status_code == 409, second.text
    forced = env.client.post(
        f"/streams/s1/dead-letters/{dl_id}/replay", headers=H, params={"force": "true"}
    )
    assert forced.status_code == 200


def test_replaying_a_poison_payload_is_422_not_500(env):
    """I1: ``dlq.replay`` re-raises ``EnvelopeRejected`` for a payload the envelope parser refuses
    — the commonest dead letter there is — and the route used to let it reach FastAPI as a 500
    with a traceback in the service log. It is the caller's stored message that is unreplayable,
    so it is a 422, naming the reason and never the payload."""
    dl_id = _seed_dl("", "s1", payload="[1, 2, 3]")  # a JSON array: not an envelope
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "this dead letter cannot be replayed: invalid_message"
    assert "[1, 2, 3]" not in r.text
    assert env.ingress.calls == []  # nothing ever reached the ingress


def test_replaying_a_payload_over_the_bindings_cap_is_422(env):
    """The narrower path into the same hole: ``limits.max_bytes`` lowered after the dead letter
    was stored makes the parser reject it as oversize."""
    upsert_stream(
        "",
        "s1",
        connector="kafka",
        model="JPCP",
        alias="Production",
        address="topic-x",
        connection=None,
        options={},
        limits={"max_bytes": 4},
        state="enabled",
        origin="api",
        actor=None,
    )
    dl_id = _seed_dl("", "s1", payload='{"payload": {"x": 1234567890}}')
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "this dead letter cannot be replayed: oversize"


def test_replay_with_no_stored_payload_is_409(env):
    dl_id = _seed_dl("", "s1", payload=None)
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H)
    assert r.status_code == 409


def test_replay_unknown_id_or_stream_is_404(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    assert env.client.post("/streams/nope/dead-letters/1/replay", headers=H).status_code == 404
    assert env.client.post("/streams/s1/dead-letters/999999/replay", headers=H).status_code == 404
    _stream("", "s2")
    other_id = _seed_dl("", "s2", payload='{"x": 1}')
    assert (
        env.client.post(f"/streams/s1/dead-letters/{other_id}/replay", headers=H).status_code == 404
    )
    assert dl_id != other_id


def test_replay_auth_matrix(env):
    dl_id = _seed_dl("", "s1", payload='{"x": 1}')
    assert (
        env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers={}).status_code == 401
    )
    r = env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=HI)
    assert r.status_code == 403 and "scope" in r.json()["detail"]
    assert env.client.post(f"/streams/s1/dead-letters/{dl_id}/replay", headers=H).status_code == 200


def test_replay_federated_auth_matrix(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    _stream("other", "s1")
    dl_id = _seed_dl("proj", "s1", payload='{"x": 1}')
    c = federated.client
    # a plain viewer never holds the raw `write` scope at all
    assert (
        c.post(
            f"/streams/s1/dead-letters/{dl_id}/replay", headers=VIEW, params={"project": "proj"}
        ).status_code
        == 403
    )
    # an operator token with no relation on the project: the fixed 403, identical for a real and
    # an unknown stream
    denied = c.post(
        f"/streams/s1/dead-letters/{dl_id}/replay", headers=OP, params={"project": "proj"}
    )
    missing = c.post("/streams/nope/dead-letters/1/replay", headers=OP, params={"project": "proj"})
    assert denied.status_code == missing.status_code == 403
    assert denied.json() == missing.json()
    other = c.post(
        f"/streams/s1/dead-letters/{dl_id}/replay", headers=OP, params={"project": "other"}
    )
    assert other.status_code == 403 and other.json() == denied.json()
    federated.grant("editor", "proj")
    assert (
        c.post(
            f"/streams/s1/dead-letters/{dl_id}/replay", headers=OP, params={"project": "proj"}
        ).status_code
        == 200
    )
    federated.decision["allow"] = False
    vetoed = c.post(
        f"/streams/s1/dead-letters/{dl_id}/replay", headers=OP, params={"project": "proj"}
    )
    assert vetoed.status_code == 403 and "center says no" in vetoed.json()["detail"]


# ── DELETE /streams/{name}/dead-letters ─────────────────────────────────────────────────────


def _age_dl(days: float) -> None:
    """Backdate every dead-letter row's ``created_at`` by ``days`` (UTC text, as the column
    holds) — ``CURRENT_TIMESTAMP`` is second-granular, so a purge cutoff computed moments after
    seeding cannot be trusted to land strictly after it without this."""

    from examlops.data import get_db

    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("UPDATE dataplane_stream_dead_letters SET created_at=?", (stamp,))


def test_purge_returns_the_count(env):
    for i in range(3):
        _seed_dl("", "s1", error=f"e{i}")
    _age_dl(2)
    r = env.client.delete("/streams/s1/dead-letters", headers=H, params={"older_than": "1"})
    assert r.status_code == 200 and r.json() == {"project": "", "name": "s1", "purged": 3}
    assert env.client.get("/streams/s1/dead-letters", headers=H).json()["items"] == []


def test_purge_accepts_a_plain_number_of_days_and_an_iso_duration(env):
    _seed_dl("", "s1")
    r = env.client.delete("/streams/s1/dead-letters", headers=H, params={"older_than": "7"})
    assert r.status_code == 200 and r.json()["purged"] == 0  # nothing is 7 days old yet
    _age_dl(1)
    r2 = env.client.delete("/streams/s1/dead-letters", headers=H, params={"older_than": "PT1H"})
    assert r2.status_code == 200 and r2.json()["purged"] == 1


def test_purge_rejects_a_bad_older_than(env):
    for bad in ("", "not-a-duration", "-5"):
        r = env.client.delete("/streams/s1/dead-letters", headers=H, params={"older_than": bad})
        assert r.status_code == 422, (bad, r.text)


def test_purge_unknown_and_other_project_stream_is_404(env):
    assert (
        env.client.delete(
            "/streams/nope/dead-letters", headers=H, params={"older_than": "0"}
        ).status_code
        == 404
    )
    assert (
        env.client.delete(
            "/streams/s1/dead-letters",
            headers=H,
            params={"older_than": "0", "project": "other"},
        ).status_code
        == 404
    )


def test_purge_auth_matrix(env):
    assert (
        env.client.delete(
            "/streams/s1/dead-letters", headers={}, params={"older_than": "0"}
        ).status_code
        == 401
    )
    r = env.client.delete("/streams/s1/dead-letters", headers=HI, params={"older_than": "0"})
    assert r.status_code == 403 and "scope" in r.json()["detail"]
    assert (
        env.client.delete(
            "/streams/s1/dead-letters", headers=H, params={"older_than": "0"}
        ).status_code
        == 200
    )


def test_purge_federated_auth_matrix(federated, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    _stream("proj", "s1")
    _stream("other", "s1")
    c = federated.client
    assert (
        c.delete(
            "/streams/s1/dead-letters", headers=VIEW, params={"project": "proj", "older_than": "0"}
        ).status_code
        == 403
    )  # a plain viewer never holds the raw `write` scope
    denied = c.delete(
        "/streams/s1/dead-letters", headers=OP, params={"project": "proj", "older_than": "0"}
    )
    missing = c.delete(
        "/streams/nope/dead-letters", headers=OP, params={"project": "proj", "older_than": "0"}
    )
    assert denied.status_code == missing.status_code == 403
    assert denied.json() == missing.json()
    other = c.delete(
        "/streams/s1/dead-letters", headers=OP, params={"project": "other", "older_than": "0"}
    )
    assert other.status_code == 403 and other.json() == denied.json()
    federated.grant("editor", "proj")
    assert (
        c.delete(
            "/streams/s1/dead-letters", headers=OP, params={"project": "proj", "older_than": "0"}
        ).status_code
        == 200
    )
    federated.decision["allow"] = False
    vetoed = c.delete(
        "/streams/s1/dead-letters", headers=OP, params={"project": "proj", "older_than": "0"}
    )
    assert vetoed.status_code == 403 and "center says no" in vetoed.json()["detail"]


# ── the DB sink itself (wiring: record() raises on a DB failure) ───────────────────────────


def test_db_sink_raises_when_the_write_fails(monkeypatch):
    """The Kafka connector's retry-until-written contract (module docstring, ruling R16) depends
    on this: ``record()`` must propagate a database failure, never swallow it."""

    def boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(catalog, "upsert_dead_letter", boom)
    sink = DbDeadLetterSink()
    binding = StreamBinding(
        project="",
        name="s1",
        connector="kafka",
        model="JPCP",
        alias="Production",
        address="topic",
        connection=None,
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        sink.record(binding, reason="oversize", error="e", attempts=1, payload=None, origin={})


# ── pause semantics per connector kind (real StreamSupervisor, fake connectors) ─────────────


def _binding(name: str = "k1", **kw: Any) -> StreamBinding:
    base: dict[str, Any] = {
        "project": "proj",
        "name": name,
        "connector": "fake",
        "model": "JPCP",
        "alias": "Production",
        "address": "topic-a",
        "connection": None,
        "options": {},
        "limits": StreamLimits(),
    }
    base.update(kw)
    return StreamBinding(**base)


class Catalog:
    def __init__(self, *bindings: StreamBinding) -> None:
        self.rows = list(bindings)

    def __call__(self) -> list[StreamBinding]:
        return list(self.rows)

    def set(self, *bindings: StreamBinding) -> None:
        self.rows = list(bindings)


class PausableConnector:
    """Kafka-shaped: ``pause()``/``resume()`` never stop ``run()`` — group membership (here: the
    run loop itself) survives a pause."""

    kind = "fake"
    connection_kinds: tuple[str, ...] = ()

    def __init__(self, *, singleton: bool = False) -> None:
        self.singleton = singleton
        self.runs = 0
        self.paused_calls: list[Any] = []
        self.resumed_calls: list[Any] = []
        self.pause_should_fail = False
        self.stop_events: list[threading.Event] = []

    def run(self, binding, ingress, stop_event, status_cb) -> None:
        self.runs += 1
        self.stop_events.append(stop_event)
        status_cb("running", None)
        stop_event.wait(30)

    def pause(self, binding: Any = None) -> None:
        if self.pause_should_fail:
            raise RuntimeError("pause boom")
        self.paused_calls.append(binding)

    def resume(self, binding: Any = None) -> None:
        self.resumed_calls.append(binding)


class UnpausableConnector:
    """No ``pause()``/``resume()`` seam at all: a pause must stop it, like ``disabled``."""

    kind = "fake"
    connection_kinds: tuple[str, ...] = ()
    singleton = False

    def __init__(self) -> None:
        self.runs = 0
        self.stop_events: list[threading.Event] = []

    def run(self, binding, ingress, stop_event, status_cb) -> None:
        self.runs += 1
        self.stop_events.append(stop_event)
        status_cb("running", None)
        stop_event.wait(30)


def _resolver(connector: Any):
    def resolve(kind: str) -> Any:
        if kind == "fake":
            return connector
        raise SpecError(f"unknown stream connector {kind!r}")

    return resolve


def _supervisor(catalog_fn: Catalog, connector: Any, **kw: Any) -> StreamSupervisor:
    kw.setdefault("sync_pack", None)
    kw.setdefault("stop_join_s", 5.0)
    kw.setdefault("reconcile_interval_s", 3600.0)  # this test drives reconcile() by hand
    return StreamSupervisor(
        ingress=object(),
        coord=kw.pop("coord", DbCoordinator()),
        list_bindings=catalog_fn,
        resolve_connector=_resolver(connector),
        **kw,
    )


def _until(pred, timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


@pytest.fixture
def cleanup():
    sups: list[StreamSupervisor] = []
    yield sups.append
    for s in sups:
        s.stop(5.0)
        s.release_leases()


def test_a_pausable_connector_is_paused_in_place_and_never_restarted(cleanup):
    conn = PausableConnector()
    cat = Catalog(_binding())
    sup = _supervisor(cat, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)

    cat.set(_binding(state="paused"))
    sup.reconcile()
    sup.reconcile()  # a second pass must not fight the pause: still no restart
    assert conn.paused_calls and conn.runs == 1  # never stopped, never restarted
    assert not conn.stop_events[0].is_set()

    cat.set(_binding(state="enabled"))
    sup.reconcile()
    assert conn.resumed_calls and conn.runs == 1  # resumed in place, not double-started


def test_pause_stream_and_resume_stream_apply_at_once(cleanup):
    conn = PausableConnector()
    sup = _supervisor(Catalog(_binding()), conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)

    sup.pause_stream("proj", "k1")
    assert len(conn.paused_calls) == 1 and conn.runs == 1

    sup.resume_stream("proj", "k1")
    assert len(conn.resumed_calls) == 1 and conn.runs == 1


def test_pause_and_resume_stream_are_a_noop_when_nothing_is_running(cleanup):
    conn = PausableConnector()
    sup = _supervisor(Catalog(), conn)
    cleanup(sup)
    sup.pause_stream("proj", "nope")  # never raises
    sup.resume_stream("proj", "nope")
    assert sup.status_of("proj", "nope") is None  # nothing was created
    assert conn.paused_calls == [] and conn.resumed_calls == []  # nothing was touched


def test_a_connector_without_a_pause_seam_is_stopped_and_restarted_on_resume(cleanup):
    conn = UnpausableConnector()
    cat = Catalog(_binding())
    sup = _supervisor(cat, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)

    cat.set(_binding(state="paused"))
    sup.reconcile()
    assert conn.stop_events[0].is_set()
    assert _until(lambda: sup.status_of("proj", "k1")["state"] == "stopped")

    cat.set(_binding(state="enabled"))
    sup.reconcile()
    assert _until(lambda: conn.runs == 2)  # started fresh


def test_a_broken_pause_stops_the_stream_instead_of_wedging_it(cleanup):
    conn = PausableConnector()
    conn.pause_should_fail = True
    cat = Catalog(_binding())
    sup = _supervisor(cat, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)
    cat.set(_binding(state="paused"))
    sup.reconcile()
    assert conn.stop_events[0].is_set()  # pause() raised: fall back to stop, as if unsupported


def test_a_singleton_that_supports_pause_keeps_its_leader_lease_while_paused(cleanup):
    conn = PausableConnector(singleton=True)
    cat = Catalog(_binding())
    sup = _supervisor(cat, conn, leader_ttl_s=5.0, holder="host-a:1:aaaa")
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)
    assert _until(lambda: sup.status_of("proj", "k1")["leader"] is True)

    cat.set(_binding(state="paused"))
    sup.reconcile()
    assert conn.paused_calls
    assert sup.status_of("proj", "k1")["leader"] is True  # never gave the lease back
    assert conn.runs == 1  # the run thread never returned

    cat.set(_binding(state="enabled"))
    sup.reconcile()
    assert conn.resumed_calls
    assert sup.status_of("proj", "k1")["leader"] is True
    assert conn.runs == 1  # still the same run: no hand-over, no restart


def test_the_drain_stops_a_paused_stream_cleanly(cleanup):
    """Controller ruling: the drain must stop a paused stream too — ``StreamSupervisor.stop()``
    signals every live run regardless of its pause state."""
    conn = PausableConnector()
    cat = Catalog(_binding())
    sup = _supervisor(cat, conn)
    cleanup(sup)
    sup.reconcile()
    assert _until(lambda: conn.runs == 1)
    sup.pause_stream("proj", "k1")
    assert conn.paused_calls and not conn.stop_events[0].is_set()
    sup.stop(5.0)
    assert conn.stop_events[0].is_set()
