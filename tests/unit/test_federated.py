"""E7 — federated & privacy-preserving training (ADR 0040).

Given/When/Then coverage of the requirements: raw data never leaves a site (only signed
weight summaries aggregate), unauthorized/unsigned sites are rejected + audited, FedAvg is
sample-weighted, the 'robust' strategy tolerates a Byzantine outlier, DP tracks an honest
(ε, δ) budget (and claims nothing when off), and secure aggregation hides per-site updates.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _u(site, weights, n, loss=0.0, signed=True):
    from examlops.federated import SiteUpdate

    return SiteUpdate(site, weights, n, loss=loss, signed=signed)


def test_init_registers_sites_and_run():
    # GWT-8: init records the run + sites with authorization state.
    from examlops.federated import federated_init, federated_status

    run = federated_init(["a", "b"], strategy="fedavg", run_id="r1")
    assert run.run_id == "r1"
    st = federated_status("r1")
    assert {s["site"] for s in st["sites"]} == {"a", "b"}
    assert all(s["authorized"] for s in st["sites"])


def test_invalid_strategy_rejected():
    from examlops.federated import federated_init

    with pytest.raises(ValueError):
        federated_init(["a"], strategy="nope")


def test_fedavg_is_sample_weighted():
    # GWT-2: FedAvg weights each site by its sample count.
    from examlops.federated import federated_init, run_round

    federated_init(["a", "b"], run_id="r1")
    res = run_round("r1", [_u("a", [0.0], 100), _u("b", [1.0], 300)])
    # weighted mean = (0*100 + 1*300)/400 = 0.75
    assert res.global_weights[0] == pytest.approx(0.75)
    assert res.sites_participated == 2


def test_raw_data_never_crosses_only_summaries():
    # GWT-1: SiteUpdate carries only weights/loss/num_samples — no raw records field exists.
    from examlops.federated import SiteUpdate

    fields = set(SiteUpdate.__dataclass_fields__)
    assert fields == {"site", "weights", "num_samples", "loss", "signed"}


def test_unauthorized_site_rejected_and_audited():
    # GWT-4: an unauthorized site is rejected and an audit event is written.
    from examlops import platform_db
    from examlops.federated import federated_init, run_round

    federated_init(["a", "b"], run_id="r1", authorized_sites=["a"])
    res = run_round("r1", [_u("a", [1.0], 10), _u("b", [9.0], 10)])
    assert res.rejected == ["b"]
    assert res.sites_participated == 1
    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT action FROM audit_events WHERE action='federated_site_rejected'"
        ).fetchall()
    assert len(rows) == 1


def test_unsigned_update_rejected():
    # GWT-4: an unsigned update from an otherwise-authorized site is rejected.
    from examlops.federated import federated_init, run_round

    federated_init(["a", "b"], run_id="r1")
    res = run_round("r1", [_u("a", [1.0], 10), _u("b", [2.0], 10, signed=False)])
    assert res.rejected == ["b"]


def test_all_rejected_raises():
    from examlops.federated import SiteRejectedError, federated_init, run_round

    federated_init(["a"], run_id="r1", authorized_sites=[])
    with pytest.raises(SiteRejectedError):
        run_round("r1", [_u("a", [1.0], 10)])


def test_robust_strategy_tolerates_byzantine_outlier():
    # GWT-6: trimmed-mean aggregation drops a poisoned extreme update.
    from examlops.federated import federated_init, run_round

    federated_init(["a", "b", "c"], strategy="robust", run_id="r1")
    res = run_round("r1", [_u("a", [1.0], 10), _u("b", [1.0], 10), _u("c", [1000.0], 10)])
    # min+max trimmed → only the two 1.0s survive → 1.0, not dragged toward 1000.
    assert res.global_weights[0] == pytest.approx(1.0)


def test_dp_budget_accumulates_across_rounds():
    # GWT-3: ε grows by epsilon_per_round each round (basic composition).
    from examlops.federated import federated_init, privacy_budget, run_round

    federated_init(["a"], run_id="r1", dp={"epsilon_per_round": 0.5, "delta": 1e-5})
    run_round("r1", [_u("a", [1.0], 10)])
    run_round("r1", [_u("a", [1.0], 10)])
    b = privacy_budget("r1")
    assert b["dp_enabled"] is True
    assert b["epsilon"] == pytest.approx(1.0)
    assert b["delta"] == 1e-5
    assert b["rounds"] == 2


def test_privacy_budget_honest_when_dp_off():
    # GWT-5: with DP off, no ε/δ is claimed.
    from examlops.federated import federated_init, privacy_budget, run_round

    federated_init(["a"], run_id="r1")
    run_round("r1", [_u("a", [1.0], 10)])
    b = privacy_budget("r1")
    assert b["dp_enabled"] is False
    assert "note" in b


def test_secure_agg_hides_per_site_updates():
    # GWT-7: under secure aggregation the coordinator sees no per-site breakdown.
    from examlops.federated import federated_init, run_round

    federated_init(["a", "b"], run_id="sec", secure_agg=True)
    res = run_round("sec", [_u("a", [1.0], 10), _u("b", [2.0], 10)])
    assert res.per_site is None

    federated_init(["a", "b"], run_id="open", secure_agg=False)
    res2 = run_round("open", [_u("a", [1.0], 10), _u("b", [2.0], 10)])
    assert res2.per_site is not None and len(res2.per_site) == 2


def test_unknown_run_raises():
    from examlops.federated import run_round

    with pytest.raises(ValueError):
        run_round("ghost", [_u("a", [1.0], 10)])
