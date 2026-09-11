# tests/unit/test_slo_prometheus_source.py
"""ADR 0023 clause 3 — the `prometheus` SLI source: an SLO's PromQL ratio, read back.

The recorded finding: "`prometheus` (needs a live Prometheus — `exa slo generate` emits the
recording rules for it to evaluate)". `exa slo ingest` now evaluates the spec's ratio as an instant
query against `PROMETHEUS_URL` and records one time-sample of it. These run a real HTTP server that
answers in the Prometheus HTTP API's format.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.platform_db import init_db, upsert_slo_spec  # noqa: E402


class _Prom:
    def __init__(self):
        self.reply: dict = _vector(0.995)
        self.queries: list[str] = []
        self.paths: list[str] = []


def _vector(*values, status="success"):
    return {
        "status": status,
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1757600000.0, str(v)]} for v in values],
        },
    }


@pytest.fixture
def prom(monkeypatch):
    state = _Prom()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            state.paths.append(u.path)
            state.queries.append(parse_qs(u.query).get("query", [""])[0])
            body = json.dumps(state.reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("PROMETHEUS_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("EXAMLOPS_SLO_PROBE_TIMEOUT", "2")
    init_db()
    yield state
    httpd.shutdown()


def _spec(query=None, name="latency"):
    upsert_slo_spec("JPCP", name, sli_source="prometheus", target=0.99, sli_query=query)


def _ingest(name="latency"):
    return next(r for r in slo.ingest_slis("JPCP") if r["name"] == name)


def test_the_spec_query_is_evaluated_and_its_ratio_recorded(prom):
    query = 'sum(rate(ok_total{model="JPCP"}[5m])) / sum(rate(all_total{model="JPCP"}[5m]))'
    _spec(query)

    row = _ingest()

    assert row["ingested"] is True and (row["good"], row["total"]) == (0.995, 1.0)
    assert prom.paths == ["/api/v1/query"] and prom.queries == [query]


def test_without_a_query_the_recorded_series_is_read(prom):
    """The same default `exa slo generate` builds its rules from."""
    _spec(None)

    _ingest()

    assert prom.queries == ['examlops:sli_ratio{model="JPCP",slo="latency"}']


def test_samples_average_over_time(prom):
    _spec("up")
    for v in (1.0, 0.9, 0.95, 1.0):
        prom.reply = _vector(v)
        slo.ingest_slis("JPCP")

    st = slo.slo_status("JPCP", "latency")[0]
    assert st.n == 4 and st.sli == pytest.approx((1.0 + 0.9 + 0.95 + 1.0) / 4)


def test_a_scalar_result_is_accepted(prom):
    prom.reply = {"status": "success", "data": {"resultType": "scalar", "result": [0, "0.8"]}}
    _spec("vector(0.8)")

    assert _ingest()["good"] == 0.8


@pytest.mark.parametrize(
    "reply,why",
    [
        (_vector(0.9, 0.99), "returned 2 series"),
        (_vector(), "returned 0 series"),
        (_vector("NaN"), "not a ratio"),
        (_vector(1.7), "not a ratio"),
        ({"status": "error", "error": "parse error at char 3"}, "refused the query: parse error"),
    ],
)
def test_an_answer_that_is_not_one_ratio_is_refused_not_averaged(prom, reply, why):
    prom.reply = reply
    _spec("x")

    row = _ingest()

    assert row["ingested"] is False and why in row["reason"]
    assert slo.slo_status("JPCP", "latency")[0].measured is False


def test_an_unreachable_prometheus_is_unmeasured_not_a_bad_sample(prom, monkeypatch):
    """A monitoring outage is not a service outage — the opposite rule to the availability
    probe, where an unreachable model is exactly what is being counted."""
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:1")
    _spec("up")

    row = _ingest()

    assert row["ingested"] is False and "could not be queried" in row["reason"]
    assert slo.slo_status("JPCP", "latency")[0].measured is False


def test_the_host_comes_from_the_environment_not_the_spec(prom):
    """The spec supplies PromQL only; there is no field through which it can name a host."""
    _spec("http://169.254.169.254/latest/meta-data/")

    _ingest()

    assert prom.paths == ["/api/v1/query"], "the one request went to PROMETHEUS_URL"
