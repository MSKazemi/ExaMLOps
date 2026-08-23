"""The promotion gate may not claim an SLO verdict it never obtained.

``maybe_promote`` writes an audit event whose ``reason`` ends in the literal words ``SLO OK``.
That claim is produced by ``_slo_ok``, which answered *every* failure with ``True``: the module
missing, the store unreadable, the query raising — all of it arrived at the promotion gate as
"no SLO regression". A safety check that cannot return a negative verdict is not a safety check,
and here the fabrication does not stop at a dashboard: it is written into the audit log, which is
the one record meant to be trustworthy after the fact.

The split these tests pin down is between *absent* and *broken*. The docstring licensed
fail-open for "C6 absent" — an optional feature nobody installed — and that stays. A check that
ran and crashed is a different fact, and promotion is the risky direction, so it blocks.
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


def _ready_challenger(model: str = "JPCP") -> None:
    """A challenger that beats its champion decisively — policy_met but for the SLO term."""
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow

    enable_shadow(model, "18", 100, min_samples=10, alpha=0.05, min_delta=0.05)
    for i in range(30):
        # Deliberate spread on both arms. Constant predictions give both samples zero variance,
        # which makes Welch degenerate (p = 1.0) and `significant` False — so the gate short-
        # circuits before it ever consults the SLO term and every assertion below would pass
        # without testing anything. `test_gwt5_slo_regression_blocks_promotion` is written that
        # way today.
        platform_db.record_challenger_sample(
            model,
            request_hash=f"h{i}",
            champion_pred=2.0 + (i % 5) * 0.1,
            challenger_pred=1.0 + (i % 5) * 0.01,
            label=1.0,
        )


def test_a_crashed_slo_check_does_not_read_as_no_regression(monkeypatch):
    """The store is unreadable. That is not evidence the error budget is intact."""
    import examlops.slo as slo_mod
    from examlops.champion_challenger import maybe_promote

    _ready_challenger()

    def _boom(*a, **k):
        raise RuntimeError("slo store unreadable")

    monkeypatch.setattr(slo_mod, "slo_status", _boom)
    assert maybe_promote("JPCP") is None


def test_the_reason_does_not_claim_slo_ok_when_no_slo_was_checked():
    """No SLO is configured, so nothing established that any budget is intact."""
    from examlops.champion_challenger import maybe_promote

    _ready_challenger()
    proposal = maybe_promote("JPCP")
    assert proposal is not None, "no SLO configured must not block promotion"
    assert "SLO OK" not in proposal.reason, (
        f"reason claims an SLO verdict that was never obtained: {proposal.reason!r}"
    )


def test_the_reason_does_not_claim_slo_ok_when_the_slo_has_no_samples():
    """An SLO with zero samples scores a perfect SLI. Absence of data is not compliance."""
    from examlops.champion_challenger import maybe_promote
    from examlops.slo import apply_spec

    _ready_challenger()
    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})

    proposal = maybe_promote("JPCP")
    assert proposal is not None, "an unmeasured SLO must not block promotion"
    assert "SLO OK" not in proposal.reason, (
        f"reason claims a budget verdict from zero samples: {proposal.reason!r}"
    )


# ── controls: these hold on both the old and the new code ────────────────────────────────


def test_an_exhausted_budget_still_blocks_promotion():
    from examlops import platform_db
    from examlops.champion_challenger import maybe_promote
    from examlops.slo import apply_spec

    _ready_challenger()
    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})
    platform_db.record_slo_sample("JPCP", "quality", good=50, total=100)

    assert maybe_promote("JPCP") is None


def test_a_healthy_measured_budget_still_permits_promotion():
    from examlops import platform_db
    from examlops.champion_challenger import maybe_promote
    from examlops.slo import apply_spec

    _ready_challenger()
    apply_spec({"model": "JPCP", "name": "quality", "target": 0.90})
    platform_db.record_slo_sample("JPCP", "quality", good=100, total=100)

    proposal = maybe_promote("JPCP")
    assert proposal is not None
