# tests/unit/test_slo_availability_probe.py
"""ADR 0023 clause 3 — the `availability` SLI source: a black-box serving-readiness probe.

The recorded finding: "`availability` (no serving-availability probe is persisted anywhere)". Each
`exa slo ingest` now asks the platform's own Ray Serve the Open Inference Protocol readiness
question — `GET /v2/models/{model}/ready` — and records one good/bad sample. These run a real
HTTP server on an ephemeral port whose answer the test controls.
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.platform_db import init_db, upsert_slo_spec  # noqa: E402


class _Serving:
    """What the fake Ray Serve answers, per path, and which paths were asked."""

    def __init__(self):
        self.status = 200
        self.asked: list[str] = []
        self.redirect_to: str | None = None


@pytest.fixture
def serving(monkeypatch, tmp_path):
    state = _Serving()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state.asked.append(self.path)
            if state.redirect_to and self.path != state.redirect_to:
                self.send_response(302)
                self.send_header("Location", state.redirect_to)
            else:
                self.send_response(state.status)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("RAY_SERVE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("EXAMLOPS_SLO_PROBE_TIMEOUT", "2")
    init_db()
    yield state
    httpd.shutdown()


def _spec(query=None, model="JPCP"):
    upsert_slo_spec(model, "up", sli_source="availability", target=0.99, sli_query=query)


def _ingest(model="JPCP"):
    return next(r for r in slo.ingest_slis(model) if r["name"] == "up")


def test_a_ready_model_is_a_good_sample(serving):
    _spec()

    row = _ingest()

    assert row["ingested"] is True and (row["good"], row["total"]) == (1.0, 1.0)
    assert serving.asked == ["/v2/models/JPCP/ready"]


@pytest.mark.parametrize("status", [400, 503, 204])
def test_a_model_that_is_not_ready_is_a_bad_sample(serving, status):
    """OIP: loaded-and-able-to-infer is exactly 200; anything else, a 204 included, is not."""
    serving.status = status
    _spec()

    row = _ingest()

    assert (row["good"], row["total"]) == (0.0, 1.0)


def test_an_unreachable_server_is_a_bad_sample_not_a_missing_one(serving, monkeypatch):
    """A model nobody can reach is exactly what an availability SLO exists to count."""
    monkeypatch.setenv("RAY_SERVE_URL", "http://127.0.0.1:1")
    _spec()

    row = _ingest()

    assert row["ingested"] is True and (row["good"], row["total"]) == (0.0, 1.0)


def test_probes_accumulate_into_the_availability_ratio(serving):
    """Each ingest is one probe; a schedule of ingests is a schedule of probes."""
    _spec()
    for status in (200, 503, 200, 200):
        serving.status = status
        slo.ingest_slis("JPCP")

    st = slo.slo_status("JPCP", "up")[0]
    assert st.n == 4 and st.sli == pytest.approx(3 / 4)


def test_a_version_can_be_pinned(serving):
    _spec(query="version:17")

    _ingest()

    assert serving.asked == ["/v2/models/JPCP/versions/17/ready"]


def test_a_redirect_is_not_followed_and_is_not_ready(serving):
    """The redirect points at a path on the same server that answers 200, so following it would
    turn this sample good — the answer must be the probed endpoint's own."""
    serving.redirect_to = "/v2/models/SOMETHING-ELSE/ready"
    _spec()

    row = _ingest()

    assert (row["good"], row["total"]) == (0.0, 1.0)
    assert serving.asked == ["/v2/models/JPCP/ready"]


@pytest.mark.parametrize(
    "query",
    ["http://169.254.169.254/latest/meta-data/", "version:../../admin", "latency_ms<=800"],
)
def test_a_spec_cannot_choose_what_the_platform_fetches(serving, query):
    """A URL taken from an SLO spec and fetched by the platform would be an SSRF primitive."""
    _spec(query=query)

    row = _ingest()

    assert row["ingested"] is False and "only --query version:<v>" in row["reason"]
    assert serving.asked == []


def test_the_model_name_cannot_escape_its_path_segment(serving):
    _spec(model="a/../../admin")

    _ingest(model="a/../../admin")

    assert serving.asked == ["/v2/models/a%2F..%2F..%2Fadmin/ready"]


@pytest.mark.parametrize(
    "model,query", [("..", None), ("JPCP", "version:.."), ("JPCP", "version:.")]
)
def test_a_dot_segment_is_refused_rather_than_normalised_away(serving, model, query):
    _spec(model=model, query=query)

    row = _ingest(model=model)

    assert row["ingested"] is False and "does not name a model" in row["reason"]
    assert serving.asked == []


def test_availability_is_no_longer_reported_as_unbuilt():
    assert "availability" in slo.SUPPORTED_SOURCES
    assert "availability" not in slo.UNSUPPORTED_SOURCES
