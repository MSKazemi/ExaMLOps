"""Per-tenant quotas travel to the serving gateway inside the serving snapshot (ADR 0123 d3).

Everything runs through the real code: the ``serving_quotas`` store, the snapshot compiler and
publisher (against the same fake MLflow REST client ``test_serving_snapshot`` uses), the replica's
digest verification, and the gateway's ``decide`` — no stubbed quota lookup.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops import gateway, serving_gateway, serving_snapshot
from examlops.cli.main import app as exa_app
from examlops.data import serving_quotas
from examlops.events import schemas
from examlops.platform_db import get_db, init_db
from serving.ray_serving.snapshot import verified
from tests.unit.test_serving_snapshot import _FakeMlflow

INFER = "/v2/models/jpcp/infer"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    serving_snapshot._version_cache.clear()
    serving_gateway._key_cache.clear()
    serving_gateway.reset_quota_cache()
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "0")
    monkeypatch.setenv("EXAMLOPS_GATEWAY_QUOTA_REFRESH_SECONDS", "0")  # re-read every decision
    yield
    serving_gateway.reset_quota_cache()


def _publish() -> int:
    generation, _ = serving_snapshot.publish(
        serving_snapshot.compile_snapshot(client=_FakeMlflow(n=2), mlflow_url="http://m")
    )
    return generation


def _bearer(tenant: str) -> str:
    return "Bearer " + gateway.issue_virtual_key(tenant, "p", None, None, "test")


def _outbox(topic: str) -> list[dict]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT payload FROM event_outbox WHERE topic=?", (topic,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ─── the store ────────────────────────────────────────────────────────────────


def test_set_update_remove_round_trip_and_publish_events():
    assert serving_quotas.set_quota("acme", 120, updated_by="t") == "created"
    assert serving_quotas.set_quota("acme", 60) == "updated"
    assert serving_quotas.get_quota("acme")["rpm"] == 60
    assert serving_quotas.remove_quota("acme") is True
    assert serving_quotas.remove_quota("acme") is False  # nothing to remove: no event either
    assert serving_quotas.get_quota("acme") is None
    events = _outbox("serving.quota_changed")
    assert [e["rpm"] for e in events] == [120, 60, None]
    assert events[-1]["removed"] is True
    for e in events:  # the payloads satisfy the registered event schema
        schemas.validate("serving.quota_changed", e)


@pytest.mark.parametrize("bad", [-1, 1.5, "10", True, None])
def test_an_invalid_rpm_is_refused_and_writes_nothing(bad):
    with pytest.raises(ValueError):
        serving_quotas.set_quota("acme", bad)  # type: ignore[arg-type]
    assert serving_quotas.list_quotas() == []
    assert _outbox("serving.quota_changed") == []


def test_a_blank_tenant_is_refused():
    with pytest.raises(ValueError):
        serving_quotas.set_quota("  ", 5)


def test_the_database_itself_rejects_a_negative_quota():
    init_db()
    with pytest.raises(Exception, match="(?i)check|constraint"):
        with get_db() as conn:
            conn.execute("INSERT INTO serving_quotas (tenant, rpm) VALUES ('x', -3)")


# ─── the snapshot carries them ────────────────────────────────────────────────


def test_the_snapshot_carries_quotas_and_a_change_makes_a_new_generation():
    serving_quotas.set_quota("acme", 120)
    first = _publish()
    body = serving_snapshot.latest()
    assert body["quotas"] == {"tenants": {"acme": {"rpm": 120}}}
    assert _publish() == first  # unchanged content: no new generation
    serving_quotas.set_quota("acme", 30)
    assert _publish() > first


def test_a_quota_event_moves_the_projector_trigger_watermark():
    before = serving_snapshot.trigger_watermark()
    serving_quotas.set_quota("acme", 5)
    assert serving_snapshot.trigger_watermark() > before


def test_a_replica_verifies_new_and_pre_quota_snapshots_and_refuses_tampering():
    serving_quotas.set_quota("acme", 120)
    _publish()
    current = serving_snapshot.latest()
    assert verified(current) is not None
    # A generation published before quotas existed hashed only three sections; it still verifies.
    legacy = {k: v for k, v in current.items() if k != "quotas"}
    legacy["digest"] = serving_snapshot.digest_of(
        {k: legacy[k] for k in ("models", "traffic", "shadow")}
    )
    assert verified(legacy) is not None
    # Rewriting a quota, or stripping the section, breaks the digest.
    raised = json.loads(json.dumps(current))
    raised["quotas"]["tenants"]["acme"]["rpm"] = 10**6
    assert verified(raised) is None
    stripped = {k: v for k, v in current.items() if k != "quotas"}
    assert verified(stripped) is None


# ─── the gateway enforces them ────────────────────────────────────────────────


def test_a_tenant_quota_from_the_snapshot_is_enforced_and_others_keep_the_default():
    serving_quotas.set_quota("acme", 2)
    _publish()
    auth = _bearer("acme")
    assert serving_gateway.decide("POST", INFER, auth).allowed
    assert serving_gateway.decide("POST", INFER, auth).allowed
    third = serving_gateway.decide("POST", INFER, auth)
    assert third.status == 429 and third.headers["retry-after"] == "60"
    # Default is 0 (off) in this test, so another tenant is untouched.
    other = _bearer("globex")
    assert all(serving_gateway.decide("POST", INFER, other).allowed for _ in range(5))


def test_a_zero_quota_makes_a_tenant_unlimited_against_a_finite_default(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "1")
    serving_quotas.set_quota("batch", 0)
    _publish()
    auth = _bearer("batch")
    assert all(serving_gateway.decide("POST", INFER, auth).allowed for _ in range(4))
    assert serving_gateway.decide("POST", INFER, _bearer("acme")).allowed
    assert serving_gateway.decide("POST", INFER, _bearer("acme")).status == 429  # default = 1


def test_a_new_generation_is_picked_up_and_removal_falls_back_to_the_default():
    serving_quotas.set_quota("acme", 1)
    _publish()
    auth = _bearer("acme")
    assert serving_gateway.decide("POST", INFER, auth).allowed
    assert serving_gateway.decide("POST", INFER, auth).status == 429
    serving_quotas.remove_quota("acme")
    _publish()
    assert serving_gateway.tenant_limit("acme") == 0


def test_the_quota_is_cached_between_refreshes(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GATEWAY_QUOTA_REFRESH_SECONDS", "3600")
    serving_quotas.set_quota("acme", 7)
    _publish()
    assert serving_gateway.tenant_limit("acme") == 7
    serving_quotas.set_quota("acme", 9)
    _publish()
    assert serving_gateway.tenant_limit("acme") == 7  # inside the refresh window
    serving_gateway.reset_quota_cache()
    assert serving_gateway.tenant_limit("acme") == 9


def test_an_unreachable_store_keeps_the_last_known_quota(monkeypatch):
    serving_quotas.set_quota("acme", 3)
    _publish()
    assert serving_gateway.tenant_limit("acme") == 3

    def down():
        raise ConnectionError("datastore unreachable")

    monkeypatch.setattr(serving_snapshot, "latest_generation", down)
    assert serving_gateway.tenant_limit("acme") == 3  # neither lifted nor invented


def test_a_process_that_never_read_a_snapshot_enforces_only_the_default(monkeypatch):
    def down():
        raise ConnectionError("datastore unreachable")

    monkeypatch.setattr(serving_snapshot, "latest_generation", down)
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "11")
    assert serving_gateway.tenant_limit("acme") == 11


def test_a_snapshot_failing_its_digest_is_not_enforced():
    serving_quotas.set_quota("acme", 1)
    _publish()
    init_db()
    with get_db() as conn:  # corrupt the stored body behind the digest's back
        row = conn.execute("SELECT generation, body FROM serving_snapshots").fetchone()
        body = json.loads(row["body"])
        body["quotas"]["tenants"]["acme"]["rpm"] = 999
        conn.execute(
            "UPDATE serving_snapshots SET body=? WHERE generation=?",
            (json.dumps(body), row["generation"]),
        )
    assert serving_gateway.tenant_limit("acme") == 0  # the default; 999 was never believed


def test_malformed_quota_entries_are_ignored_not_fatal():
    _publish()
    content = {k: v for k, v in serving_snapshot.latest().items() if k != "generation"}
    content["quotas"] = {"tenants": {"a": {"rpm": -4}, "b": {"rpm": "x"}, "c": {"rpm": 5}, "d": 1}}
    content["digest"] = serving_snapshot.digest_of(
        {k: content[k] for k in serving_snapshot.CONTENT_KEYS}
    )
    serving_snapshot.publish(content)
    assert serving_gateway._snapshot_quotas() == {"c": 5}


# ─── the CLI ──────────────────────────────────────────────────────────────────


def test_the_cli_sets_lists_and_removes_a_quota():
    runner = CliRunner()
    out = runner.invoke(exa_app, ["--json", "gateway", "quota", "set", "acme", "40"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["result"] == "created"
    listed = json.loads(runner.invoke(exa_app, ["--json", "gateway", "quota", "list"]).stdout)
    assert [(r["tenant"], r["rpm"]) for r in listed] == [("acme", 40)]
    assert runner.invoke(exa_app, ["gateway", "quota", "remove", "acme"]).exit_code == 0
    assert runner.invoke(exa_app, ["gateway", "quota", "remove", "acme"]).exit_code != 0
    assert runner.invoke(exa_app, ["gateway", "quota", "set", "acme", "-1"]).exit_code == 2
