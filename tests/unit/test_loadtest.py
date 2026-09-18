"""The open-loop load test measures what an overloaded server does to its callers (plan P5).

``examlops.loadtest`` sends requests on a fixed schedule and times each from when it was due. The
test that matters most is the overloaded server: a closed-loop tester would slow down with it and
report the server's service time; this one must report the queueing its callers really suffer.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from examlops import loadtest

URL = "http://serving.test/v2/models/jpcp/infer"
BODY = {"inputs": [{"name": "x", "shape": [1], "datatype": "FP64", "data": [0.0]}]}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(handler, **kwargs) -> loadtest.LoadReport:
    async def go():
        async with _client(handler) as client:
            return await loadtest.run(URL, BODY, client=client, **kwargs)

    return asyncio.run(go())


def test_requests_leave_on_schedule():
    report = _run(lambda request: httpx.Response(200, json={}), rate=200, duration=0.5)
    assert report.sent == 100 and report.succeeded == 100 and report.dropped == 0
    assert 150 <= report.achieved_rate <= 210


def test_an_overloaded_server_shows_its_queue_not_its_service_time():
    """Capacity 50/s (one at a time, 20 ms each), offered 100/s: the queue grows all run long."""
    lock = asyncio.Lock()

    async def one_at_a_time(request):
        async with lock:
            await asyncio.sleep(0.02)
        return httpx.Response(200, json={})

    report = _run(one_at_a_time, rate=100, duration=0.6)
    assert report.succeeded == report.sent == 60
    # A closed-loop tester would report ~20 ms. Timed from the schedule, the last requests waited
    # behind ~30 others.
    assert report.percentile(50) > 100
    assert report.percentile(99) > 400


def test_statuses_sheds_and_transport_errors_are_counted():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] % 4 == 0:
            return httpx.Response(503, json={"error": "overloaded"})
        if calls["n"] % 5 == 0:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    report = _run(flaky, rate=100, duration=0.4)
    assert report.sent == 40
    assert report.statuses[503] == 10 and report.shed == 10
    assert report.transport_errors == 6  # every 5th call not already a 4th: 5,10,…,40 minus 20,40
    assert report.failed == 16 and report.error_rate == pytest.approx(0.4)


def test_a_client_that_cannot_keep_up_invalidates_the_run():
    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    report = _run(slow, rate=100, duration=0.3, max_in_flight=2)
    assert report.dropped > 0
    assert any(b.startswith("client_saturated") for b in report.breaches())


def test_breaches_name_the_slo_that_failed():
    report = loadtest.LoadReport(target_rate=1, duration_s=1, sent=10)
    report.statuses[200] = 9
    report.statuses[500] = 1
    report.latencies_ms = [10.0] * 8 + [900.0]
    assert report.breaches(p99_ms=1000, max_error_rate=0.2) == []
    found = report.breaches(p99_ms=500, max_error_rate=0.05)
    assert [b.split(":")[0] for b in found] == ["error_rate", "p99"]


def test_nearest_rank_percentiles():
    report = loadtest.LoadReport(target_rate=1, duration_s=1)
    report.latencies_ms = [float(v) for v in range(1, 101)]
    assert (report.percentile(50), report.percentile(99), report.percentile(100)) == (50, 99, 100)


def test_a_request_is_built_from_a_column_signature():
    metadata = {
        "inputs": [
            {"name": "num_nodes", "datatype": "INT64", "shape": [-1]},
            {"name": "mem", "datatype": "FP64", "shape": [-1]},
        ]
    }
    body = loadtest.body_from_metadata(metadata, alias="Canary")
    assert body == {
        "inputs": [
            {"name": "num_nodes", "shape": [1], "datatype": "INT64", "data": [0]},
            {"name": "mem", "shape": [1], "datatype": "FP64", "data": [0.0]},
        ],
        "parameters": {"alias": "Canary"},
    }


def test_a_model_without_a_signature_needs_a_body():
    metadata = {"inputs": [{"name": "input-0", "datatype": "FP64", "shape": [-1, -1]}]}
    with pytest.raises(ValueError, match="--body"):
        loadtest.body_from_metadata(metadata)


# ── the command ───────────────────────────────────────────────────────────────


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    from examlops.cli.main import app

    seen: dict = {}

    async def fake_run(url, body, **kwargs):
        seen.update(url=url, body=body, headers=kwargs["headers"], rate=kwargs["rate"])
        report = loadtest.LoadReport(target_rate=kwargs["rate"], duration_s=kwargs["duration"])
        report.sent = 10
        report.statuses[200] = 10
        report.latencies_ms = [5.0] * 9 + [800.0]
        report.elapsed_s = 1.0
        return report

    monkeypatch.setattr(loadtest, "run", fake_run)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(BODY))
    return CliRunner(), app, seen, str(request)


def test_the_command_passes_and_fails_on_the_slos(cli, monkeypatch):
    runner, app, seen, request = cli
    monkeypatch.setenv("EXAMLOPS_LOADTEST_TOKEN", "-".join(("exa", "test", "key")))
    args = ["--json", "serve", "loadtest", "jpcp", "--body", request, "--url", "http://gw:18088"]
    passed = runner.invoke(app, [*args, "--p99-ms", "1000"])
    assert passed.exit_code == 0, passed.output
    assert json.loads(passed.output)["passed"] is True
    assert seen["url"] == "http://gw:18088/v2/models/jpcp/infer"
    assert seen["headers"] == {"Authorization": "Bearer exa-test-key"}  # from the env only

    failed = runner.invoke(app, [*args, "--p99-ms", "100"])
    assert failed.exit_code == 1
    verdict = json.loads(failed.output)
    assert verdict["passed"] is False and verdict["breaches"][0].startswith("p99")


def test_the_alias_rides_in_the_request(cli):
    runner, app, seen, request = cli
    result = runner.invoke(app, ["--json", "serve", "loadtest", "jpcp", "--body", request,
                                 "--alias", "Canary"])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert seen["body"]["parameters"] == {"alias": "Canary"}
