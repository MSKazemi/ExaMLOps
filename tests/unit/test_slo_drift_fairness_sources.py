# tests/unit/test_slo_drift_fairness_sources.py
"""Two SLI sources that were named in ADRs and had no ingester.

ADR 0023 clause 3 names `c5` (drift) as an SLI source; `exa slo ingest` did not recognise the
string at all, so a spec declaring it was reported as an *unknown* source — the message said the
source did not exist rather than that it was unbuilt.

ADR 0025 clause 3 says fairness thresholds "surface as a C6 fairness SLI + alert". Nothing
turned a disparity into an SLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.data.drift import record_drift_event  # noqa: E402
from examlops.data.governance import set_fairness_config  # noqa: E402
from examlops.slo import SUPPORTED_SOURCES, apply_spec, ingest_slis  # noqa: E402


def _spec(model: str, name: str, source: str, query: str | None = None) -> None:
    apply_spec(
        {
            "model": model,
            "name": name,
            "target": 0.9,
            "window": "30d",
            "sli_source": source,
            "sli_query": query,
        }
    )


def _row(model: str, name: str) -> dict:
    return next(r for r in ingest_slis(model) if r["name"] == name)


# ── c5: drift ─────────────────────────────────────────────────────────────────


def test_recorded_drift_verdicts_become_an_sli():
    for severity in ("OK", "OK", "OK", "CRITICAL"):
        record_drift_event("DriftA", "concept", severity=severity)
    _spec("DriftA", "not-drifting", "c5")

    row = _row("DriftA", "not-drifting")

    assert row["ingested"] is True
    assert (row["good"], row["total"]) == (3.0, 4.0)


def test_the_query_pins_one_drift_kind():
    record_drift_event("DriftB", "concept", severity="OK")
    record_drift_event("DriftB", "label", severity="CRITICAL")
    record_drift_event("DriftB", "label", severity="CRITICAL")
    _spec("DriftB", "label-only", "c5", query="label")

    row = _row("DriftB", "label-only")

    assert (row["good"], row["total"]) == (0.0, 2.0)


def test_a_warning_is_not_good():
    """Only OK counts as good — a WARNING is a detection the SLO should feel."""
    record_drift_event("DriftC", "feature", severity="OK")
    record_drift_event("DriftC", "feature", severity="WARNING")
    _spec("DriftC", "s", "c5")
    assert _row("DriftC", "s")["good"] == 1.0


def test_no_recorded_verdicts_reports_why_rather_than_nothing():
    """A source with no data must not read downstream as a healthy service."""
    _spec("DriftD", "s", "c5")
    row = _row("DriftD", "s")
    assert row["ingested"] is False
    assert "prediction drift is computed live and records none" in row["reason"]


def test_c5_was_previously_reported_as_an_unknown_source():
    """The regression this closes: the ADR named c5 and the module did not know the string."""
    assert "c5" in SUPPORTED_SOURCES


def test_a_genuine_typo_is_reported_as_such_and_lists_the_real_sources():
    _spec("DriftE", "s", "c55")
    row = _row("DriftE", "s")
    assert row["ingested"] is False
    assert "unrecognised sli_source 'c55'" in row["reason"]
    assert "c5" in row["reason"] and "prometheus" in row["reason"]


def test_every_source_the_schema_names_now_has_an_ingester():
    """This test used to pin an unbuilt source's reason (`availability`, then `prometheus`).
    With both built (BL-061, BL-063) none is left: every `sli_source` the specs accept ingests."""
    from examlops import slo as slo_mod

    assert not slo_mod.UNSUPPORTED_SOURCES
    for source in ("c1", "c2", "c5", "c8", "availability", "prometheus"):
        assert source in slo_mod.SUPPORTED_SOURCES, source


# ── c8: fairness ──────────────────────────────────────────────────────────────


@pytest.fixture
def fair(monkeypatch, tmp_path):
    """A model with a declared slice registry, and controllable fairness results."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "fair.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "FairM",
                "config_class": "c",
                "task_type": "regression",
                "fairness": {"slices": ["region", "tier"], "min_samples": 5},
            }
        )
    )
    monkeypatch.setattr("examlops.usecase.models_dir", lambda *a, **k: models)
    return "FairM"


def _result(attr: str, *, exceeded: bool, measured: bool = True):
    from examlops.fairness import FairnessResult

    return FairnessResult(
        model="FairM",
        slice_attr=attr,
        tenant="default",
        demographic_parity_diff=0.3 if measured else None,
        disparity_exceeded=exceeded,
    )


def test_declared_attributes_within_threshold_are_the_good_events(fair, monkeypatch):
    monkeypatch.setattr(
        "examlops.fairness.fairness_report",
        lambda model, **kw: [
            _result("region", exceeded=False),
            _result("tier", exceeded=True),
        ],
    )
    _spec(fair, "fair", "c8")

    row = _row(fair, "fair")

    assert row["ingested"] is True
    assert (row["good"], row["total"]) == (1.0, 2.0)


def test_an_unmeasurable_attribute_is_excluded_not_counted_as_good(fair, monkeypatch):
    """Counting it as good would let a model with no data score a perfect fairness SLI."""
    monkeypatch.setattr(
        "examlops.fairness.fairness_report",
        lambda model, **kw: [
            _result("region", exceeded=False),
            _result("tier", exceeded=False, measured=False),
        ],
    )
    _spec(fair, "fair2", "c8")

    row = _row(fair, "fair2")

    assert (row["good"], row["total"]) == (1.0, 1.0), "the unmeasured attribute must not count"


def test_nothing_measurable_reports_why(fair, monkeypatch):
    monkeypatch.setattr(
        "examlops.fairness.fairness_report",
        lambda model, **kw: [_result("region", exceeded=False, measured=False)],
    )
    _spec(fair, "fair3", "c8")
    row = _row(fair, "fair3")
    assert row["ingested"] is False
    assert "enough" in row["reason"] and "min_samples=5" in row["reason"]


def test_a_model_with_no_slice_registry_reports_that(monkeypatch, tmp_path):
    empty = tmp_path / "models"
    empty.mkdir()
    monkeypatch.setattr("examlops.usecase.models_dir", lambda *a, **k: empty)
    _spec("NoFair", "fair", "c8")
    row = _row("NoFair", "fair")
    assert row["ingested"] is False
    assert "declares no slice registry" in row["reason"]


def test_the_fairness_sli_reads_a_runtime_registry_too(monkeypatch, tmp_path):
    """It resolves through the same effective-config path the gate uses, not a second lookup."""
    empty = tmp_path / "models"
    empty.mkdir()
    monkeypatch.setattr("examlops.usecase.models_dir", lambda *a, **k: empty)
    set_fairness_config("DbFair", ["region"], min_samples=5)
    monkeypatch.setattr(
        "examlops.fairness.fairness_report",
        lambda model, **kw: [_result("region", exceeded=False)],
    )
    _spec("DbFair", "fair", "c8")
    assert _row("DbFair", "fair")["ingested"] is True


# ── the ingest still behaves like the rest of the module ──────────────────────


def test_a_breach_is_reported_by_the_ingest(monkeypatch):
    record_drift_event("DriftG", "concept", severity="CRITICAL")
    _spec("DriftG", "strict", "c5")
    seen = {}

    def _record(model, name, good, total, **kw):
        seen.update({"good": good, "total": total, "source": kw.get("source")})
        return True

    monkeypatch.setattr(slo, "record_sample", _record)
    row = _row("DriftG", "strict")
    assert row["breached"] is True
    assert seen["source"] == "exa-slo-ingest"
