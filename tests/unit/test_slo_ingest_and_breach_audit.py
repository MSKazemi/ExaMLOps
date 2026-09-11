"""ADR 0023 clauses 3 and 5 — SLIs come from the platform, and a breach leaves a record.

Two gaps were recorded against this ADR on 2026-08-28. Clause 3: every SLI arrived by hand
through `exa slo record`, and the `sli_source` column the specs already carried was read by
nothing. Clause 5's last third: an SLO breach was **not audited** — `exa slo status` would show a
spent error budget with no D4 record of it ever having been spent, which is the one event a
governance layer exists to keep.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.data.evaluation import record_eval_result  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    init_db()


def _spec(name="quality", target=0.9, source="c2", query="pass_rate", gate=False):
    slo.apply_spec(
        {
            "model": "JPCP",
            "name": name,
            "target": target,
            "window": "30d",
            "sli_source": source,
            "sli_query": query,
            "gate_promotion": gate,
        }
    )


def _breach_events():
    return [e for e in export_audit_events() if e["action"] == "slo_breached"]


# ── clause 5: the breach is audited ───────────────────────────────────────────


def test_a_breach_writes_an_audit_event():
    _spec(target=0.9)
    assert slo.record_sample("JPCP", "quality", 50, 100) is True  # 0.50 vs target 0.90
    events = _breach_events()
    assert len(events) == 1
    assert events[0]["target"] == "JPCP/quality"


def test_the_event_carries_the_numbers_that_explain_it():
    """An audit row saying only 'a breach happened' cannot be acted on later."""
    import json

    _spec(target=0.9)
    slo.record_sample("JPCP", "quality", 50, 100)
    details = _breach_events()[0]["details"]
    d = json.loads(details) if isinstance(details, str) else details
    assert d["target"] == 0.9
    assert d["sli"] == pytest.approx(0.5)
    assert d["samples"] == 100
    assert d["budget_remaining"] < 0


def test_a_healthy_sample_audits_nothing():
    _spec(target=0.9)
    assert slo.record_sample("JPCP", "quality", 99, 100) is False
    assert _breach_events() == []


def test_only_the_transition_is_audited_not_every_later_sample():
    """One row per interval for as long as a breach lasts is an audit trail nobody reads."""
    _spec(target=0.9)
    slo.record_sample("JPCP", "quality", 0, 100)
    assert len(_breach_events()) == 1
    slo.record_sample("JPCP", "quality", 0, 100)
    slo.record_sample("JPCP", "quality", 0, 100)
    assert len(_breach_events()) == 1, "a sustained breach re-audited itself"


def test_the_cli_record_path_audits_too(monkeypatch):
    """A hand-typed sample that spends the last of a budget is still a breach."""
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _spec(target=0.9)
    res = CliRunner().invoke(app, ["slo", "record", "JPCP", "quality", "10", "100"])
    assert res.exit_code == 0, res.output
    assert len(_breach_events()) == 1


# ── clause 3: SLIs come from the platform ─────────────────────────────────────


def test_an_eval_result_becomes_an_sli_sample():
    """`eval_suite_results` is already a proportion over a known sample size — the one shape an
    SLI needs."""
    _spec(target=0.8, query="pass_rate")
    record_eval_result("smoke", "JPCP", {"pass_rate": 0.95}, run_id="r1", sample_size=200)

    rows = slo.ingest_slis("JPCP")
    assert rows[0]["ingested"] is True
    assert (rows[0]["good"], rows[0]["total"]) == (190, 200.0)
    assert slo.slo_status("JPCP", "quality")[0].measured is True


def test_a_suite_can_be_pinned_with_suite_colon_metric():
    _spec(query="agent-safety:answer_rate")
    record_eval_result("other", "JPCP", {"answer_rate": 0.10}, run_id="r1", sample_size=10)
    record_eval_result("agent-safety", "JPCP", {"answer_rate": 1.0}, run_id="r2", sample_size=10)
    rows = slo.ingest_slis("JPCP")
    assert rows[0]["ingested"] is True and rows[0]["good"] == 10


def test_ingestion_that_breaches_reports_and_audits_it():
    _spec(target=0.9, query="pass_rate")
    record_eval_result("smoke", "JPCP", {"pass_rate": 0.10}, run_id="r1", sample_size=100)
    rows = slo.ingest_slis("JPCP")
    assert rows[0]["breached"] is True
    assert len(_breach_events()) == 1


# ── the skips are the point ───────────────────────────────────────────────────


# `availability` left this list when it gained a probe (BL-061): it probes and records a sample
# rather than skipping. `c1` stays — with no --query it refuses rather than guessing.
@pytest.mark.parametrize("source", ["c1", "prometheus"])
def test_a_source_with_no_ingester_says_so_instead_of_recording_nothing(source):
    """Silence here would read downstream as *unmeasured*, and 'we have no ingester for this'
    must not look the same as 'the service is healthy and nobody asked'."""
    _spec(source=source, query="")
    row = slo.ingest_slis("JPCP")[0]
    assert row["ingested"] is False
    assert row["reason"] and len(row["reason"]) > 20
    assert slo.slo_status("JPCP", "quality")[0].measured is False


def test_c2_without_a_query_explains_what_is_missing():
    _spec(query="")
    row = slo.ingest_slis("JPCP")[0]
    assert row["ingested"] is False and "--query" in row["reason"]


def test_a_metric_that_is_not_a_ratio_is_refused():
    """An SLI is good/total. A unit-bearing metric cannot back one, and rounding it into a count
    would invent a denominator."""
    _spec(query="latency_p95")
    record_eval_result(
        "smoke",
        "JPCP",
        {"latency_p95": 30.0},
        run_id="r1",
        sample_size=100,
        non_proportion_metrics={"latency_p95"},
    )
    row = slo.ingest_slis("JPCP")[0]
    assert row["ingested"] is False and "not a ratio" in row["reason"]


def test_a_missing_metric_is_named_in_the_reason():
    _spec(query="nonexistent_rate")
    row = slo.ingest_slis("JPCP")[0]
    assert row["ingested"] is False and "nonexistent_rate" in row["reason"]
