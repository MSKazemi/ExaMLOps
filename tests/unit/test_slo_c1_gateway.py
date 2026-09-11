# tests/unit/test_slo_c1_gateway.py
"""ADR 0023 clause 3 — the `c1` SLI source: gateway latency and errors.

The recorded finding: "`c1` (gateway calls record cost and tokens but neither latency nor an
error flag)". So no latency or error SLO could be measured from the gateway at all, and a request
that failed on every backend left no row, which makes any error rate computed from the table zero
by construction. Calls now record `latency_ms` and `error`, and `c1` specs ingest from them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops import slo  # noqa: E402
from examlops.data.gateway import record_gateway_call  # noqa: E402
from examlops.platform_db import get_db, init_db, upsert_slo_spec  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


def _calls(model="llm"):
    with get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT latency_ms, error, backend FROM gateway_calls WHERE model=? ORDER BY id",
                (model,),
            )
        ]


def _ok(model, messages, **kw):
    return gw.Completion(text="hi", model=model, backend="", prompt_tokens=3, completion_tokens=2)


def _down(model, messages, **kw):
    raise RuntimeError("backend down")


def _client(*backends):
    r = gw.Router()
    r.add_route("llm", list(backends))
    return gw.GatewayClient(r)


# ── the gateway now measures what the caller experiences ─────────────────────


def test_a_served_call_records_its_latency_and_no_error():
    _client(("primary", _ok)).chat("llm", [{"role": "user", "content": "x"}])

    (row,) = _calls()
    assert row["error"] == 0 and row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_a_request_every_backend_failed_is_recorded_as_an_error():
    """It used to leave no row, so any error rate computed from the table was zero."""
    with pytest.raises(gw.AllBackendsFailed):
        _client(("a", _down), ("b", _down)).chat("llm", [{"role": "user", "content": "x"}])

    (row,) = _calls()
    assert row["error"] == 1 and row["latency_ms"] is not None and row["backend"] is None


def test_a_failover_that_succeeded_is_one_successful_call():
    _client(("a", _down), ("b", _ok)).chat("llm", [{"role": "user", "content": "x"}])

    assert [r["error"] for r in _calls()] == [0]


# ── the c1 ingester ──────────────────────────────────────────────────────────


def _spec(name, query, window="30d"):
    upsert_slo_spec("llm", name, sli_source="c1", target=0.9, sli_query=query, window=window)


def _ingest(name):
    return next(r for r in slo.ingest_slis("llm") if r["name"] == name)


def test_a_latency_slo_counts_successful_calls_within_the_threshold():
    for ms in (120, 300, 790, 1500):
        record_gateway_call(None, "llm", latency_ms=ms)
    record_gateway_call(None, "llm", latency_ms=50, error=True)  # the error SLI's business
    record_gateway_call(None, "llm")  # written before latency was measured: unmeasured
    _spec("p-latency", "latency_ms<=800")

    row = _ingest("p-latency")

    assert row["ingested"] is True and (row["good"], row["total"]) == (3.0, 4.0)


def test_an_error_slo_counts_calls_that_did_not_fail():
    for err in (False, False, False, True):
        record_gateway_call(None, "llm", latency_ms=100, error=err)
    _spec("errors", "errors")

    row = _ingest("errors")

    assert (row["good"], row["total"]) == (3.0, 4.0)


def test_calls_outside_the_window_do_not_count():
    record_gateway_call(None, "llm", latency_ms=100)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO gateway_calls (model, latency_ms, error, ts) "
            "VALUES ('llm', 5000, 0, '2000-01-01 00:00:00')"
        )
    _spec("p-latency", "latency_ms<=800", window="7d")

    assert (_ingest("p-latency")["good"], _ingest("p-latency")["total"]) == (1.0, 1.0)


@pytest.mark.parametrize(
    "query,window,why",
    [
        ("", "30d", "c1 needs --query"),
        ("p99 < 800", "30d", "got 'p99 < 800'"),
        ("errors", "a month", "is not of the form"),
    ],
)
def test_an_unusable_spec_says_why(query, window, why):
    _spec("bad", query or None, window=window)

    row = _ingest("bad")

    assert row["ingested"] is False and why in row["reason"]


def test_no_measured_calls_is_unmeasured_not_healthy():
    record_gateway_call(None, "llm")  # a pre-measurement row
    _spec("errors", "errors")

    row = _ingest("errors")

    assert row["ingested"] is False and "no measured gateway calls" in row["reason"]


def test_c1_is_no_longer_reported_as_unbuilt():
    assert "c1" in slo.SUPPORTED_SOURCES and "c1" not in slo.UNSUPPORTED_SOURCES


# ── an older datastore gains the columns ─────────────────────────────────────


def test_a_datastore_from_before_gains_the_columns(tmp_path, monkeypatch):
    """`get_db` opens without bootstrapping the schema, so the pre-migration table is built
    through it exactly as an older release left it, and `init_db` then migrates it in place."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "old.db"))
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE gateway_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT, "
            "model TEXT NOT NULL, backend TEXT, cost_usd REAL NOT NULL DEFAULT 0, "
            "prompt_tokens INTEGER NOT NULL DEFAULT 0, "
            "completion_tokens INTEGER NOT NULL DEFAULT 0, "
            "ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO gateway_calls (model) VALUES ('legacy')")
    init_db(force=True)

    record_gateway_call(None, "legacy", latency_ms=42.0)
    with get_db() as c:
        rows = [tuple(r) for r in c.execute("SELECT latency_ms, error FROM gateway_calls")]
    assert rows[0][0] is None, "an old row stays unmeasured"
    assert rows[1] == (42.0, 0)
