"""ADR 0116 - the admission seam: request vocabulary, policies, capability probe, Kueue render.

The decisive test is the equivalence one: the default policy must choose exactly what the
existing SQL ``claim_next_admission`` chooses, computed against the OLD function.
"""

from __future__ import annotations

import json
import random

import pytest

from examlops.admission_seam import (
    AdapterCapabilities,
    Admit,
    BaselineOverQuota,
    ClusterState,
    JobRequest,
    JobRequestError,
    KueueUnsupported,
    LegacyFairShare,
    PreemptUnsupported,
    Queue,
    QueuedItem,
    Quotas,
    Reject,
    Resources,
    TenantQuota,
    legacy_pick,
    preempt,
    probe,
    render_kueue,
    select_policy,
)
from examlops.admission_seam.kueue import FlavorSpec, flavors_from_clusters


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_ADMISSION_POLICY", raising=False)
    monkeypatch.delenv("EXAMLOPS_ADMISSION_QUOTAS", raising=False)
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


# ── JobRequest ───────────────────────────────────────────────────────────────────────────
def test_request_round_trips_through_json():
    req = JobRequest(
        project="p",
        tenant="t",
        resources=Resources(gpus=8, cpus=32, memory_gb=256.0, nodes=2),
        gang=True,
        network_tier="scale_up",
        scale_up_domain="required",
        priority_class="high",
        flexibility_s=3600.0,
        deadline="2026-12-01T00:00:00+00:00",
        est_runtime_s=7200.0,
    ).validate()
    again = JobRequest.from_dict(json.loads(json.dumps(req.to_dict())))
    assert again == req
    assert again.gpu_hours == pytest.approx(16.0)
    assert again.priority == 50


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"network_tier": "warp"}, "network_tier"),
        ({"scale_up_domain": "required"}, "needs network_tier 'scale_up'"),
        ({"priority_class": "urgent"}, "priority_class"),
        ({"flexibility_s": -1}, "flexibility_s"),
        ({"deadline": "tomorrow-ish"}, "deadline"),
        ({"gang": "yes"}, "gang must be a boolean"),
        ({"schema_version": 2}, "schema_version"),
        ({"resources": {"gpus": -1}}, "resources.gpus"),
        ({"resources": {"gpus": True}}, "resources.gpus must be an integer"),
        ({"resources": {"nodes": 0}}, "nodes must be >= 1"),
    ],
)
def test_invalid_requests_are_refused_with_every_reason(patch, needle):
    with pytest.raises(JobRequestError) as ei:
        JobRequest.from_dict({"project": "p", **patch})
    assert needle in str(ei.value)


def test_unknown_and_missing_fields_are_refused_not_ignored():
    with pytest.raises(JobRequestError, match="unknown field"):
        JobRequest.from_dict({"project": "p", "gpuz": 4})
    with pytest.raises(JobRequestError, match="unknown resources field"):
        JobRequest.from_dict({"project": "p", "resources": {"tpus": 1}})
    with pytest.raises(JobRequestError):
        JobRequest.from_dict({})


# ── default policy == old function ───────────────────────────────────────────────────────
def _old_pick(db, items, running_by_tenant, max_running, per_tenant_cap):
    """Run the REAL ``claim_next_admission`` on a fresh queue holding exactly this scenario and
    return the id it would hand out (ids are 1..n in insertion order)."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM admission_queue")
    ids = {}
    for it in items:
        ids[it.id] = db.enqueue_admission("retrain", {}, tenant=it.tenant, priority=it.priority)
    for tenant, n in running_by_tenant.items():
        for _ in range(n):
            rid = db.enqueue_admission("retrain", {}, tenant=tenant)
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE admission_queue SET state='running', "
                    "started_at=CURRENT_TIMESTAMP WHERE id=?",
                    (rid,),
                )
    got = db.claim_next_admission(max_running=max_running, per_tenant_cap=per_tenant_cap)
    back = {v: k for k, v in ids.items()}
    return None if got is None else back[got["id"]]


SCENARIOS = [
    # (queued (tenant, priority), running_by_tenant, max_running, per_tenant_cap)
    ([("a", 0), ("a", 0), ("b", 0)], {}, 4, 2),
    ([("a", 0)] * 10 + [("b", 0)], {"a": 1}, 10, 10),  # flood vs one
    ([("a", 5), ("a", 0)], {}, 4, 2),  # priority within tenant
    ([("a", 0), ("b", 9)], {"a": 0, "b": 1}, 4, 2),  # fewest-running beats priority
    ([("a", 0), ("b", 0)], {"a": 2}, 4, 2),  # a at its cap
    ([("a", 0)], {"b": 4}, 4, 2),  # global cap full
    ([("a", 3), ("b", 3), ("c", 3)], {"a": 1, "b": 1, "c": 1}, 4, 2),  # three-way tie -> oldest
    ([], {}, 4, 2),  # empty queue
    ([("a", 1), ("a", 1), ("b", 1), ("b", 2)], {"a": 1}, 3, 2),
]


@pytest.mark.parametrize("queued, running, mx, cap", SCENARIOS)
def test_default_policy_picks_what_the_old_function_picks(db, queued, running, mx, cap):
    items = [QueuedItem(id=i + 1, tenant=t, priority=p) for i, (t, p) in enumerate(queued)]
    old = _old_pick(db, items, running, mx, cap)
    new = legacy_pick(items, running, mx, cap)
    assert (new.id if new else None) == old
    # ...and through the seam object, not just the helper:
    state = ClusterState(running_total=sum(running.values()), running_by_tenant=running)
    seam = LegacyFairShare().pick_next(items, state, Quotas(max_running=mx, per_tenant_cap=cap))
    assert (seam.id if seam else None) == old


def test_default_policy_matches_the_old_function_over_a_random_sweep(db):
    rng = random.Random(1116)
    for _ in range(60):
        tenants = ["a", "b", "c"][: rng.randint(1, 3)]
        items = [
            QueuedItem(id=i + 1, tenant=rng.choice(tenants), priority=rng.randint(0, 3))
            for i in range(rng.randint(0, 8))
        ]
        running = {t: rng.randint(0, 2) for t in tenants if rng.random() < 0.6}
        mx, cap = rng.randint(1, 6), rng.randint(1, 3)
        old = _old_pick(db, items, running, mx, cap)
        new = legacy_pick(items, running, mx, cap)
        assert (new.id if new else None) == old, (items, running, mx, cap)


def test_default_decide_agrees_with_the_caps():
    req = JobRequest(project="p", tenant="a")
    pol = LegacyFairShare()
    q = Quotas(max_running=3, per_tenant_cap=2)
    assert isinstance(pol.decide(req, ClusterState(0, {}), q), Admit)
    assert isinstance(pol.decide(req, ClusterState(3, {"b": 3}), q), Queue), "global cap full"
    d = pol.decide(req, ClusterState(2, {"a": 2}), q)
    assert isinstance(d, Queue) and "concurrency cap" in d.reason
    # It never rejects: the pre-seam queue had no such verdict.
    assert not isinstance(pol.decide(req, ClusterState(9, {"a": 9}), q), Reject)


def test_default_is_selected_unless_the_env_names_another(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_ADMISSION_POLICY", raising=False)
    assert select_policy().name == "fair-share"
    monkeypatch.setenv("EXAMLOPS_ADMISSION_POLICY", "baseline-over-quota")
    assert select_policy().name == "baseline-over-quota"
    with pytest.raises(ValueError, match="unknown admission policy"):
        select_policy("round-robin")


# ── baseline vs over-quota ───────────────────────────────────────────────────────────────
Q = Quotas(
    max_running=10,
    tenants={"a": TenantQuota(baseline_gpus=4, limit_gpus=8, over_quota_weight=2.0)},
)
POL = BaselineOverQuota()
CAP_GANG = AdapterCapabilities(supports_gang=True)


def _req(gpus, **kw):
    return JobRequest(project="p", tenant="a", resources=Resources(gpus=gpus), **kw)


def test_within_baseline_is_admitted_not_over_quota():
    d = POL.decide(_req(4), ClusterState(free_gpus=16, total_gpus=16), Q)
    assert isinstance(d, Admit) and not d.over_quota


def test_beyond_baseline_borrows_idle_capacity_and_says_so():
    st = ClusterState(gpus_in_use_by_tenant={"a": 4}, free_gpus=12, total_gpus=16)
    d = POL.decide(_req(2), st, Q)
    assert isinstance(d, Admit) and d.over_quota


def test_limit_is_hard_and_capacity_queues():
    assert isinstance(POL.decide(_req(9), ClusterState(free_gpus=99), Q), Reject)
    st = ClusterState(gpus_in_use_by_tenant={"a": 7}, free_gpus=99)
    assert isinstance(POL.decide(_req(2), st, Q), Queue)  # 7+2 > limit 8: wait, do not reject
    d = POL.decide(_req(2), ClusterState(free_gpus=1, total_gpus=16), Q)
    assert isinstance(d, Queue) and "insufficient free GPUs" in d.reason
    assert isinstance(POL.decide(_req(2), ClusterState(free_gpus=1, total_gpus=1), Q), Reject)


def test_baseline_without_free_capacity_queues_and_admits_no_preemption():
    st = ClusterState(free_gpus=0, total_gpus=16)
    d = POL.decide(_req(2), st, Q)
    assert isinstance(d, Queue) and "reclamation of borrowed GPUs is not implemented" in d.reason


def test_pick_next_serves_baseline_before_borrowers_and_weights_shares():
    q = Quotas(
        tenants={
            "a": TenantQuota(baseline_gpus=2, over_quota_weight=1.0),
            "b": TenantQuota(baseline_gpus=0, over_quota_weight=4.0),
            "c": TenantQuota(baseline_gpus=0, over_quota_weight=1.0),
        }
    )
    st = ClusterState(gpus_in_use_by_tenant={"b": 4, "c": 4}, free_gpus=8)
    items = [
        QueuedItem(1, "c", gpus=2),
        QueuedItem(2, "b", gpus=2),
        QueuedItem(3, "a", gpus=2),
    ]
    assert POL.pick_next(items, st, q).id == 3  # within baseline first
    assert POL.pick_next(items[:2], st, q).id == 2  # b's 4/4 share < c's 4/1


# ── gang / topology honesty ──────────────────────────────────────────────────────────────
def test_gang_is_refused_unless_the_backend_says_it_can():
    for caps in (AdapterCapabilities(), AdapterCapabilities(supports_gang=False)):
        d = POL.decide(_req(4, gang=True), ClusterState(free_gpus=16, capabilities=caps), Q)
        assert isinstance(d, Reject) and "gang" in d.reason
    ok = POL.decide(_req(4, gang=True), ClusterState(free_gpus=16, capabilities=CAP_GANG), Q)
    assert isinstance(ok, Admit)


def test_required_scale_up_domain_queues_rather_than_spanning():
    req = _req(8, network_tier="scale_up", scale_up_domain="required")
    d = POL.decide(req, ClusterState(free_gpus=32, largest_free_domain_gpus=4), Q)
    assert isinstance(d, Queue) and "not placed spanning" in d.reason
    assert isinstance(POL.decide(req, ClusterState(free_gpus=32), Q), Reject)  # topology unknown
    assert isinstance(
        POL.decide(req, ClusterState(free_gpus=32, largest_free_domain_gpus=8), Q), Admit
    )


# ── capability probe / preempt ───────────────────────────────────────────────────────────
def test_probe_defaults_to_unknown_and_reports_the_mock_truthfully():
    class Bare:
        pass

    assert probe(Bare()) == AdapterCapabilities()  # all None
    assert probe(Bare()).supports_preempt is None

    from examlops.admission_seam.capability import register_capabilities  # noqa: F401

    class MockSlurmAdapter:  # same type name the registry keys on
        pass

    caps = probe(MockSlurmAdapter())
    assert (caps.supports_gang, caps.supports_preempt, caps.supports_reservations) == (
        False,
        False,
        False,
    )


def test_probe_reads_a_self_description_and_survives_a_broken_one():
    class Says:
        def capabilities(self):
            return {"supports_gang": True}

    class Broken:
        def capabilities(self):
            raise RuntimeError("boom")

    assert probe(Says()).supports_gang is True and probe(Says()).supports_preempt is None
    assert probe(Broken()) == AdapterCapabilities()


def test_probe_does_not_alter_the_real_adapters():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "infra" / "slurm-adapter"))
    from mock_slurm_adapter import MockSlurmAdapter
    from scheduler import SchedulerAdapter

    a = MockSlurmAdapter()
    assert isinstance(a, SchedulerAdapter)  # still satisfies the unchanged protocol
    assert not hasattr(a, "preempt") and not hasattr(a, "capabilities")
    c = probe(a)
    assert (c.supports_gang, c.supports_preempt, c.supports_reservations) == (False,) * 3


def test_preempt_is_refused_never_faked():
    class NoVerb:
        killed = False

    class Unknown:
        def preempt(self, job_id, *, checkpoint=True):  # has the verb, never declared it
            raise AssertionError("must not be called")

    with pytest.raises(PreemptUnsupported, match="has not declared"):
        preempt(Unknown(), "j1")
    with pytest.raises(PreemptUnsupported, match="does not support"):
        preempt(_Declares(False), "j1")
    with pytest.raises(PreemptUnsupported, match="no preempt"):
        preempt(_Declares(True, verb=False), "j1")
    assert preempt(_Declares(True), "j1") == "preempted:j1:True"


class _Declares:
    def __init__(self, ok, verb=True):
        self._ok = ok
        if verb:
            self.preempt = lambda job_id, *, checkpoint=True: f"preempted:{job_id}:{checkpoint}"

    def capabilities(self):
        return {"supports_preempt": self._ok}


# ── Kueue render ─────────────────────────────────────────────────────────────────────────
def test_kueue_renders_a_queue_hierarchy_and_names_what_it_does_not_promise():
    q = Quotas(tenants={"a": TenantQuota(baseline_gpus=4, limit_gpus=6)})
    docs = render_kueue(q, [FlavorSpec("h100", 32)], projects_by_tenant={"a": ["research"]})
    kinds = [d["kind"] for d in docs]
    assert kinds == ["ResourceFlavor", "AdmissionCheck", "ClusterQueue", "LocalQueue"]
    cq = docs[2]["spec"]
    res = cq["resourceGroups"][0]["flavors"][0]["resources"][0]
    assert res["nominalQuota"] == 4 and res["borrowingLimit"] == 2
    assert cq["preemption"] == {"withinClusterQueue": "Never", "reclaimWithinCohort": "Never"}
    assert docs[3]["spec"]["clusterQueue"] == "a"


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"require_gang": True}, "gang"),
        ({"require_topology": True}, "topology"),
    ],
)
def test_kueue_refuses_features_it_cannot_render(kwargs, match):
    q = Quotas(tenants={"a": TenantQuota(baseline_gpus=1)})
    with pytest.raises(KueueUnsupported, match=match):
        render_kueue(q, [FlavorSpec("h100", 8)], **kwargs)


def test_kueue_refuses_weights_multiple_flavors_and_non_kubernetes_fleets():
    weighted = Quotas(tenants={"a": TenantQuota(baseline_gpus=1, over_quota_weight=3.0)})
    with pytest.raises(KueueUnsupported, match="FairSharing"):
        render_kueue(weighted, [FlavorSpec("h", 1)])
    plain = Quotas(tenants={"a": TenantQuota(baseline_gpus=1)})
    with pytest.raises(KueueUnsupported, match="several flavors"):
        render_kueue(plain, [FlavorSpec("h", 1), FlavorSpec("l", 1)])
    with pytest.raises(KueueUnsupported, match="Slurm and Flux"):
        flavors_from_clusters([{"name": "lxp", "scheduler": "flux"}])
    assert flavors_from_clusters(
        [{"name": "k1", "scheduler": "kubernetes", "capabilities": {"total_gpus": 8}}]
    ) == [FlavorSpec("k1", 8)]
