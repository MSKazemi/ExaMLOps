# tests/unit/test_slo_ingest_incremental.py
"""ADR 0023 clause 3 — `exa slo ingest` counts each event once (BL-060).

Found 2026-09-11 and reproduced: `slo_status` sums an SLO's samples, and every ingester recorded
its **whole window** as one new sample on each run. So a daily ingest counted the same events once
per day. 120 real gateway calls read as 620 after six ingests, and an outage on the sixth day was
diluted by recounting a good month: the SLI read 0.948 against a true 0.817, exactly when the SLO
needed to be sharp. Event sources now record a watermark (`<table>:<id>`) and count only newer
events; `c8`, a point-in-time measurement, is sampled on every ingest by design.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.data.gateway import record_gateway_call  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    get_db,
    init_db,
    record_eval_result,
    upsert_slo_spec,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _c1(name="errors", query="errors"):
    upsert_slo_spec("llm", name, sli_source="c1", target=0.99, sli_query=query, window="30d")


def _calls(n, error=False):
    for _ in range(n):
        record_gateway_call(None, "llm", latency_ms=100, error=error)


def _status(name="errors", model="llm"):
    return slo.slo_status(model, name)[0]


def _ingest(name="errors", model="llm"):
    return next(r for r in slo.ingest_slis(model) if r["name"] == name)


def test_a_daily_ingest_counts_every_event_exactly_once():
    """The reproduction: a good month, five daily ingests, then an outage."""
    _c1()
    _calls(98)
    _calls(2, error=True)
    for _ in range(5):
        slo.ingest_slis("llm")
    _calls(20, error=True)
    slo.ingest_slis("llm")

    st = _status()
    assert st.n == 120, "each real call counted once"
    assert st.sli == pytest.approx(98 / 120), "the outage is not diluted by recounting"


def test_an_ingest_with_nothing_new_records_nothing_and_says_so():
    _c1()
    _calls(10)
    assert _ingest()["ingested"] is True

    row = _ingest()

    assert row["ingested"] is False and row["up_to_date"] is True
    assert "since the last ingest" in row["reason"]
    assert _status().n == 10


def test_new_events_after_an_ingest_are_the_only_ones_added():
    _c1()
    _calls(10)
    slo.ingest_slis("llm")
    _calls(3, error=True)

    row = _ingest()

    assert (row["good"], row["total"]) == (0.0, 3.0)
    assert _status().n == 13


def test_a_latency_slo_is_incremental_too():
    _c1("fast", "latency_ms<=800")
    _calls(4)
    slo.ingest_slis("llm")
    slo.ingest_slis("llm")

    assert _status("fast").n == 4


def test_c5_drift_verdicts_are_counted_once():
    from examlops.platform_db import record_drift_event

    upsert_slo_spec("jpcp", "stable", sli_source="c5", target=0.9, sli_query=None)
    for sev in ("OK", "OK", "CRITICAL"):
        record_drift_event("jpcp", "concept", severity=sev, score=0.1)
    slo.ingest_slis("jpcp")
    slo.ingest_slis("jpcp")
    record_drift_event("jpcp", "concept", severity="OK", score=0.1)
    slo.ingest_slis("jpcp")

    st = _status("stable", "jpcp")
    assert st.n == 4 and st.sli == pytest.approx(3 / 4)


def test_c2_eval_results_are_counted_once():
    upsert_slo_spec("jpcp", "quality", sli_source="c2", target=0.9, sli_query="pass_rate")
    record_eval_result("smoke", "jpcp", {"pass_rate": 0.9}, run_id="r1", sample_size=100)
    slo.ingest_slis("jpcp")
    slo.ingest_slis("jpcp")
    record_eval_result("smoke", "jpcp", {"pass_rate": 0.5}, run_id="r2", sample_size=100)
    slo.ingest_slis("jpcp")

    st = _status("quality", "jpcp")
    assert st.n == 200 and st.sli == pytest.approx((90 + 50) / 200)


def test_a_mark_from_another_source_is_no_mark():
    """A spec whose source was changed starts afresh rather than skipping by a foreign id."""
    _c1()
    _calls(5)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO slo_samples (model, tenant, name, good, total, watermark) "
            "VALUES ('llm', 'default', 'errors', 0, 0, 'drift_events:999999')"
        )

    assert _ingest()["total"] == 5.0
