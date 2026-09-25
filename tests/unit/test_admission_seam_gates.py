"""ADR 0116 decision 5 — policy, budget and carbon are consulted behind the one ``decide()`` call.

Pinned here: the default (no gate configured) changes nothing; a configured gate is enforced;
deny overrides defer overrides allow; and a gate that cannot answer denies (ADR 0108: a broker that
cannot verify reports *unverified*, never fails open). Every assertion is on the decision the seam
returns or on the audit row it wrote, not on which function was called.
"""

from __future__ import annotations

import pytest

from examlops.admission_seam import JobRequest, Resources, dispatch, gates
from examlops.admission_seam.capability import AdapterCapabilities
from examlops.admission_seam.policy import Admit, ClusterState, Queue, Quotas, Reject
from examlops.admission_seam.service import decide
from examlops.finops.carbon_signal import CarbonSignal

STATE = ClusterState(capabilities=AdapterCapabilities())
QUOTAS = Quotas(max_running=4, per_tenant_cap=2)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "cfg"))  # no site policy.yaml
    for var in (
        gates.GATES_ENV,
        gates.CARBON_MAX_ENV,
        "EXAMLOPS_ADMISSION_POLICY",
        "EXAMLOPS_ADMISSION_QUOTAS",
        dispatch.ENABLED_ENV,
        "EXAMLOPS_PROJECT",
        "EXAMLOPS_POLICY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    import examlops.platform_db as pdb
    from examlops import policy

    monkeypatch.setattr(policy, "POLICY_YAML", tmp_path / "cfg" / "policy.yaml")  # none yet
    pdb.init_db()
    return pdb


def _req(**kw):
    base = {"project": "p", "resources": Resources(gpus=2)}
    base.update(kw)
    return JobRequest(**base)


def _audit(pdb, action):
    with pdb.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_events WHERE action=? ORDER BY id", (action,)
            )
        ]


def _decide(req, **kw):
    return decide(req, state=STATE, quotas=QUOTAS, **kw)


def _signal(monkeypatch, grams, method):
    from examlops.finops import grid_intensity

    monkeypatch.setattr(
        grid_intensity,
        "current_grid_signal",
        lambda default: CarbonSignal(grams_per_kwh=grams, method=method),
    )


def _budget(monkeypatch, status):
    from examlops import project_finops

    monkeypatch.setattr(project_finops, "budget_status", lambda project, **k: status)


# ── default: nothing changes ──────────────────────────────────────────────────────────────
def test_no_gate_configured_leaves_the_decision_and_meta_untouched(db):
    decision, meta = _decide(_req())
    assert isinstance(decision, Admit)
    assert "gates" not in meta, "an unconfigured seam must look exactly as it did"
    assert _audit(db, "admission_gate_blocked") == []


def test_gates_do_not_run_on_a_request_the_policy_already_queued(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    _budget(monkeypatch, {"breaches": ["x"], "budget": {"gpu_hours_budget": 1}})
    full = ClusterState(running_total=4)
    decision, meta = decide(_req(), state=full, quotas=QUOTAS)
    assert isinstance(decision, Queue) and "global concurrency cap" in decision.reason
    assert "gates" not in meta


# ── budget ────────────────────────────────────────────────────────────────────────────────
def test_budget_gate_denies_a_breached_project_and_audits_it(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    _budget(
        monkeypatch,
        {
            "budget": {"gpu_hours_budget": 10.0},
            "breaches": ["GPU-hours 12.0 exceed budget 10.0"],
            "consumption": {"gpu_hours": 12.0},
            "period": "monthly",
        },
    )
    decision, meta = _decide(_req())
    assert isinstance(decision, Reject)
    assert "gate budget" in decision.reason and "over budget" in decision.reason
    assert meta["gates"][0]["verdict"] == "deny"
    rows = _audit(db, "admission_gate_blocked")
    assert len(rows) == 1 and rows[0]["target"] == "p"


def test_budget_gate_denies_a_request_that_would_breach(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    _budget(
        monkeypatch,
        {
            "budget": {"gpu_hours_budget": 10.0},
            "breaches": [],
            "consumption": {"gpu_hours": 9.0},
            "period": "monthly",
        },
    )
    # 2 GPUs x 1h = 2 GPU-h; 9 + 2 > 10
    decision, _ = _decide(_req(est_runtime_s=3600))
    assert isinstance(decision, Reject) and "would exceed the GPU-hour budget" in decision.reason
    # the same request with no runtime estimate asks for 0 GPU-h and fits
    decision, _ = _decide(_req())
    assert isinstance(decision, Admit)


def test_budget_gate_abstains_for_a_project_without_a_budget(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    decision, meta = _decide(_req(project="nobudget"))
    assert isinstance(decision, Admit)
    assert meta["gates"] == [
        {"gate": "budget", "verdict": "abstain", "reason": "project 'nobudget' has no budget"}
    ]


def test_budget_gate_against_the_real_finops_store(db, monkeypatch):
    """No fake: a real budget, a real recorded cost, the real ``budget_status``."""
    from examlops.data.finops import record_model_cost
    from examlops.data.projects import assign_model_to_project, create_project, set_project_budget

    create_project("real")
    set_project_budget("real", gpu_hours_budget=5.0, cost_budget=None)
    assign_model_to_project("real", "m1")
    record_model_cost("m1", 1, None, None, gpu_hours=6.0, cost_usd=None, project="real")
    monkeypatch.setenv(gates.GATES_ENV, "budget")

    decision, _ = _decide(_req(project="real"))
    assert isinstance(decision, Reject) and "over budget" in decision.reason


def test_a_budget_store_that_raises_denies_as_unverified(db, monkeypatch):
    from examlops import project_finops

    def boom(project, **k):
        raise RuntimeError("datastore down")

    monkeypatch.setattr(project_finops, "budget_status", boom)
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    decision, _ = _decide(_req())
    assert isinstance(decision, Reject)
    assert "unverified" in decision.reason and "datastore down" in decision.reason


# ── policy-as-code ───────────────────────────────────────────────────────────────────────
def _policy_file(tmp_path, monkeypatch, body):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "policy.yaml").write_text(body)
    from examlops import policy

    # POLICY_YAML is resolved once at import; point the real loader at this file.
    monkeypatch.setattr(policy, "POLICY_YAML", cfg / "policy.yaml")


def test_policy_gate_enforces_a_deny_rule_for_admission(db, tmp_path, monkeypatch):
    _policy_file(
        tmp_path,
        monkeypatch,
        "policies:\n  - name: no-admission\n    action: admission\n    effect: deny\n",
    )
    monkeypatch.setenv(gates.GATES_ENV, "policy")
    decision, meta = _decide(_req())
    assert isinstance(decision, Reject) and "gate policy" in decision.reason
    assert meta["gates"][0]["detail"] == {"rule": "no-admission"}


def test_policy_require_approval_queues_rather_than_rejects(db, tmp_path, monkeypatch):
    _policy_file(
        tmp_path,
        monkeypatch,
        "policies:\n  - action: admission\n    effect: require_approval\n",
    )
    monkeypatch.setenv(gates.GATES_ENV, "policy")
    decision, _ = _decide(_req())
    assert isinstance(decision, Queue) and "requires approval" in decision.reason


def test_policy_gate_with_no_rule_allows_on_the_engines_documented_default(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "policy")
    decision, meta = _decide(_req())
    assert isinstance(decision, Admit)
    assert meta["gates"][0]["verdict"] == "allow"


def test_a_broken_policy_engine_denies(db, monkeypatch):
    from examlops import policy

    def boom(*a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(policy, "decide", boom)
    monkeypatch.setenv(gates.GATES_ENV, "policy")
    decision, _ = _decide(_req())
    assert isinstance(decision, Reject) and "unverified" in decision.reason


# ── carbon ───────────────────────────────────────────────────────────────────────────────
def test_carbon_gate_defers_flexible_work_on_a_high_marginal_signal(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 450.0, "locational_marginal")
    decision, meta = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Queue) and "deferred" in decision.reason
    assert meta["gates"][0]["detail"]["signal_type"] == "decision"


def test_carbon_gate_never_defers_inflexible_work(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 450.0, "locational_marginal")
    decision, meta = _decide(_req(flexibility_s=0))
    assert isinstance(decision, Admit)
    assert "not flexible" in meta["gates"][0]["reason"]


def test_carbon_gate_refuses_to_shift_on_an_average_signal(db, monkeypatch):
    """ADR 0112: an accounting signal must not drive scheduling, however high it reads."""
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 900.0, "average_grid_mix")
    decision, meta = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Admit)
    assert meta["gates"][0]["verdict"] == "abstain"
    assert "no decision-grade carbon signal" in meta["gates"][0]["reason"]


def test_carbon_gate_does_not_defer_past_a_deadline(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 450.0, "marginal_emissions")
    soon = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    decision, _ = _decide(_req(flexibility_s=7200, est_runtime_s=3600, deadline=soon))
    assert isinstance(decision, Admit)


def test_carbon_gate_below_threshold_allows(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 120.0, "locational_marginal")
    decision, meta = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Admit) and meta["gates"][0]["verdict"] == "allow"


def test_carbon_gate_without_a_threshold_abstains(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    _signal(monkeypatch, 999.0, "locational_marginal")
    decision, meta = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Admit) and meta["gates"][0]["verdict"] == "abstain"


def test_a_malformed_carbon_threshold_denies(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "lots")
    decision, _ = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Reject) and "unverified" in decision.reason


# ── combination and configuration ─────────────────────────────────────────────────────────
def test_deny_overrides_defer(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "carbon,budget")
    monkeypatch.setenv(gates.CARBON_MAX_ENV, "300")
    _signal(monkeypatch, 450.0, "locational_marginal")
    _budget(monkeypatch, {"budget": {"cost_budget": 1}, "breaches": ["cost"], "period": "m"})
    decision, meta = _decide(_req(flexibility_s=3600))
    assert isinstance(decision, Reject) and "gate budget" in decision.reason
    assert [g["verdict"] for g in meta["gates"]] == ["defer", "deny"], "every gate is reported"


def test_an_unknown_gate_name_denies_instead_of_being_skipped(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget,telepathy")
    decision, _ = _decide(_req())
    assert isinstance(decision, Reject)
    assert "unverified" in decision.reason and "telepathy" in decision.reason


def test_simulation_records_nothing(db, monkeypatch):
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    _budget(monkeypatch, {"budget": {"gpu_hours_budget": 1}, "breaches": ["b"], "period": "m"})
    decision, _ = _decide(_req(), record=False)
    assert isinstance(decision, Reject)
    assert _audit(db, "admission_gate_blocked") == []


def test_a_registered_site_gate_is_consulted(db, monkeypatch):
    class BurnIn:
        name = "burn-in"

        def evaluate(self, request, *, record):
            return gates.GateResult(self.name, gates.DEFER, "node pool still in burn-in")

    monkeypatch.setitem(gates._REGISTRY, "burn-in", BurnIn)
    monkeypatch.setenv(gates.GATES_ENV, "burn-in")
    decision, _ = _decide(_req())
    assert isinstance(decision, Queue) and "burn-in" in decision.reason


def test_a_gate_returning_a_made_up_verdict_denies(db):
    class Odd:
        name = "odd"

        def evaluate(self, request, *, record):
            return gates.GateResult(self.name, "maybe", "?")

    results = gates.evaluate_gates(_req(), gates=[Odd()])
    assert results[0].verdict == "deny" and "unknown verdict" in results[0].reason


def test_dispatch_refuses_a_run_a_gate_blocks(db, monkeypatch):
    """The gate governs the real dispatch path, not only the simulator."""
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv(gates.GATES_ENV, "budget")
    _budget(monkeypatch, {"budget": {"gpu_hours_budget": 1}, "breaches": ["b"], "period": "m"})
    with pytest.raises(dispatch.AdmissionRefused) as info:
        with dispatch.admitted(_req()):
            pytest.fail("the body must not run")
    assert info.value.verdict == "reject" and "gate budget" in info.value.reason
    assert len(_audit(db, "admission_refused")) == 1
