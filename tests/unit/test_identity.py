"""examlops.identity — agent principals, scoped grants, JIT leases (ADR 0108, AIDC W3 spec §1).

The four G-W-T acceptance cases from the spec, plus the scope/expiry edges. The property under
test throughout: autonomous and delegated action are DIFFERENT CREDENTIALS, every denial is an
``authz_denied`` security event naming principal/operation/grant, and expiry is evaluated at
check time — no sweeper exists to be late.
"""

from __future__ import annotations

import pytest

from examlops import identity
from examlops.platform_db import get_db, init_db


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _denials() -> list[dict]:
    import json

    with get_db() as conn:
        rows = conn.execute(
            "SELECT target, details FROM audit_events WHERE action='authz_denied'"
        ).fetchall()
    return [{"audit_target": r["target"], **json.loads(r["details"])} for r in rows]


def _agent() -> identity.AgentPrincipal:
    return identity.register_agent("skipper", owner="mohsen", purpose="ops agent")


class TestGWT1ReadOnlyGrantBlocksWrite:
    def test_write_denied_with_security_event(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        decision = identity.check(le.lease_id, action="write", target="jpcp")
        assert not decision.allowed
        assert "outside grant scope" in decision.reason
        denial = _denials()[-1]
        # The denial carries the principal, requested operation and grant id — verbatim spec.
        assert denial["audit_target"] == a.agent_id
        assert denial["operation"] == "write"
        assert denial["grant_id"] == g.grant_id

    def test_read_allowed_under_same_lease(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        assert identity.check(le.lease_id, action="read", target="jpcp").allowed


class TestGWT2ExpiryAtCheckTime:
    def test_expired_lease_denied_without_sweeper(self, monkeypatch):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        assert identity.check(le.lease_id, action="read", target="jpcp").allowed
        # 61 seconds "elapse" — by moving the clock, not by waiting for any background job.
        from datetime import timedelta

        real_now = identity._now
        monkeypatch.setattr(identity, "_now", lambda: real_now() + timedelta(seconds=61))
        decision = identity.check(le.lease_id, action="read", target="jpcp")
        assert not decision.allowed and decision.reason == "lease expired"

    def test_lease_never_outlives_grant(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=30,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=99999)
        assert le.expires_at <= g.expires_at


class TestGWT3ModesAreDifferentCredentials:
    def test_delegated_and_autonomous_carry_different_lease_ids(self):
        a = _agent()
        g_auto = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read", "write"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        g_del = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read", "write"},
            ttl_s=600,
            mode="DELEGATED",
            on_behalf_of="mohsen",
        )
        l_auto = identity.lease(g_auto.grant_id, target="jpcp", ttl_s=60)
        l_del = identity.lease(g_del.grant_id, target="jpcp", ttl_s=60)
        assert l_auto.lease_id != l_del.lease_id
        who_auto = identity.whoami(l_auto.lease_id)
        who_del = identity.whoami(l_del.lease_id)
        assert who_auto.mode == "AUTONOMOUS" and who_auto.on_behalf_of is None
        assert who_del.mode == "DELEGATED" and who_del.on_behalf_of == "mohsen"

    def test_delegated_requires_on_behalf_of(self):
        a = _agent()
        with pytest.raises(ValueError, match="on_behalf_of"):
            identity.grant(
                a.agent_id,
                resource={"kind": "model", "ids": "*"},
                data={"sensitivity_max": "internal", "collections": "*"},
                operation={"read"},
                ttl_s=600,
                mode="DELEGATED",
            )

    def test_autonomous_forbids_on_behalf_of(self):
        a = _agent()
        with pytest.raises(ValueError, match="must not carry"):
            identity.grant(
                a.agent_id,
                resource={"kind": "model", "ids": "*"},
                data={"sensitivity_max": "internal", "collections": "*"},
                operation={"read"},
                ttl_s=600,
                mode="AUTONOMOUS",
                on_behalf_of="mohsen",
            )


class TestGWT4Decommission:
    def test_decommission_revokes_every_lease(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        l1 = identity.lease(g.grant_id, target="jpcp", ttl_s=600)
        l2 = identity.lease(g.grant_id, target="mack", ttl_s=600)
        identity.decommission_agent(a.agent_id, reason="compromised")
        for le in (l1, l2):
            d = identity.check(le.lease_id, action="read", target=le.target)
            assert not d.allowed
        with pytest.raises(ValueError, match="DECOMMISSIONED"):
            identity.grant(
                a.agent_id,
                resource={"kind": "model", "ids": "*"},
                data={"sensitivity_max": "internal", "collections": "*"},
                operation={"read"},
                ttl_s=600,
                mode="AUTONOMOUS",
            )


class TestScopesAndEdges:
    def test_lease_is_single_target(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        d = identity.check(le.lease_id, action="read", target="mack")
        assert not d.allowed and "not this lease's target" in d.reason

    def test_resource_id_scope_enforced(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": ["mack"]},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        d = identity.check(le.lease_id, action="read", target="jpcp")
        assert not d.allowed and "outside resource scope" in d.reason

    def test_revoked_lease_denied_with_reason(self):
        a = _agent()
        g = identity.grant(
            a.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        identity.revoke(le.lease_id, reason="operator action")
        d = identity.check(le.lease_id, action="read", target="jpcp")
        assert not d.allowed and "operator action" in d.reason

    def test_unknown_lease_denied_and_audited(self):
        d = identity.check("lease-nope", action="read", target="jpcp")
        assert not d.allowed
        assert _denials()[-1]["lease_id"] == "lease-nope"

    def test_unknown_operation_rejected_at_grant(self):
        a = _agent()
        with pytest.raises(ValueError, match="unknown operations"):
            identity.grant(
                a.agent_id,
                resource={"kind": "model", "ids": "*"},
                data={"sensitivity_max": "internal", "collections": "*"},
                operation={"launch-missiles"},
                ttl_s=600,
                mode="AUTONOMOUS",
            )

    def test_whoami_walks_parent_chain_and_labels_issuer(self):
        parent = identity.register_agent("orchestrator", owner="mohsen", purpose="root")
        child = identity.register_agent(
            "worker", owner="mohsen", purpose="sub", parent=parent.agent_id
        )
        g = identity.grant(
            child.agent_id,
            resource={"kind": "model", "ids": "*"},
            data={"sensitivity_max": "internal", "collections": "*"},
            operation={"read"},
            ttl_s=600,
            mode="AUTONOMOUS",
        )
        le = identity.lease(g.grant_id, target="jpcp", ttl_s=60)
        who = identity.whoami(le.lease_id)
        assert who.chain == (child.agent_id, parent.agent_id)
        assert who.issuer == "local"
