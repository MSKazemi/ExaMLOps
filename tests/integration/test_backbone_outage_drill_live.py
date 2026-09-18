"""Chaos drill: the event backbone (NATS) dies while the platform works (plan P5, ADR 0124).

A throwaway Docker network with Postgres, NATS JetStream, a Prefect stand-in and a real control
plane publishing to the bus, all from an image built from this tree. The drill kills NATS, keeps
submitting retrains, starts NATS again, and holds the backbone to what ADR 0124 promises:

- **the bus is not on the write path.** Retrains are accepted while NATS is gone: an event is a row
  in the outbox first, published afterwards, so a broker outage cannot refuse platform work;
- **the control plane stays in rotation.** A bus outage is not a readiness failure. Publishing is
  asynchronous, so pulling every replica out of service over it would turn a degraded bus into an
  unavailable API;
- **nothing is lost and nothing is doubled.** The outbox grows during the outage and drains on its
  own afterwards, with each event published once (JetStream dedupes on ``Nats-Msg-Id``);
- **the backlog is visible while it lasts** — ``examlops_event_outbox_pending`` is what the alert
  reads.

It then kills the datastore **as well**, because losing one dependency at a time is the easy case.
With both gone the platform must refuse cleanly and stay alive; when the store alone comes back it
must serve again and keep queueing what it cannot publish; and when the broker follows, the backlog
must drain exactly once.

Opt-in and slow (Docker and an image build)::

    docker build -f platform/services/control_plane/Dockerfile \\
        -t exa-chaos/examlops-control-plane:tree .
    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_backbone_outage_drill_live.py -v -s
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import time
import uuid

import httpx
import pytest

from tests.integration.test_control_plane_failover_kind_live import PREFECT_STUB

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
]

IMAGE = "exa-chaos/examlops-control-plane:tree"
POSTGRES = "postgres:17-alpine"
NATS = "nats:2.14.6-alpine"
PYTHON = "python:3.12-slim"
TIMINGS: dict[str, object] = {}
# One retrain per pair: two of the same model and dataset in flight at once is a 409 by design.
PAIRS = (("JPCP", "PM100Dataset"), ("JPCP", "FDataDataset"), ("MACK", "FDataDataset"))


def _run(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    out = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args[:6])}…: {out.stderr[-2000:]}")
    return out


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(fn, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fn()
        if found:
            return found
        time.sleep(interval)
    return None


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if _run("docker", "image", "inspect", IMAGE, check=False).returncode:
        pytest.skip(f"build {IMAGE} first (see the module docstring)")
    tag = uuid.uuid4().hex[:8]
    net = f"exa-bus-{tag}"
    names = {k: f"exa-bus-{k}-{tag}" for k in ("pg", "nats", "prefect", "cp")}
    tmp = tmp_path_factory.mktemp("bus")
    tmp.chmod(0o755)
    password, token = secrets.token_hex(16), secrets.token_hex(24)
    cp_port = _port()
    try:
        _run("docker", "network", "create", net)
        # No --rm on the broker: a killed container keeps its JetStream store, so `docker start`
        # brings the stream back as a real restart would.
        _run(
            "docker", "run", "-d", "--name", names["nats"], "--network", net,
            "--network-alias", "nats", NATS, "-js", "-sd", "/data", "-m", "8222",
        )  # fmt: skip
        # No --rm on the store either: the combined-failure section kills and restarts it.
        _run(
            "docker", "run", "-d", "--name", names["pg"], "--network", net,
            "--network-alias", "postgres", "-e", "POSTGRES_USER=examlops",
            "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=examlops", POSTGRES,
        )  # fmt: skip
        (tmp / "prefect.py").write_text(PREFECT_STUB)
        (tmp / "prefect.py").chmod(0o644)
        _run(
            "docker", "run", "-d", "--rm", "--name", names["prefect"], "--network", net,
            "--network-alias", "prefect", "-v", f"{tmp}:/w:ro", PYTHON, "python", "/w/prefect.py",
        )  # fmt: skip
        assert _until(
            lambda: _run("docker", "exec", names["pg"], "pg_isready", "-U", "examlops",
                         check=False).returncode == 0,
            timeout=60,
        ), "postgres never came up"  # fmt: skip
        _run(
            "docker", "run", "-d", "--rm", "--name", names["cp"], "--network", net,
            "-p", f"127.0.0.1:{cp_port}:8002",
            "-e", "EXAMLOPS_DB_BACKEND=postgres",
            "-e", f"EXAMLOPS_POSTGRES_DSN=postgresql://examlops:{password}@postgres:5432/examlops",
            "-e", "EXAMLOPS_EVENT_PUBLISHER=nats", "-e", "EXAMLOPS_NATS_URL=nats://nats:4222",
            "-e", "CONTROL_PLANE_EVENT_RELAY_SECONDS=1", "-e", f"CONTROL_PLANE_TOKEN={token}",
            "-e", "PREFECT_API_URL=http://prefect:4200/api", "-e", "MODELZOO_POLL_SECONDS=0",
            "-e", "CONTROL_PLANE_RECONCILE_SECONDS=1", "-e", "OTEL_SDK_DISABLED=true",
            "-e", "EXAMLOPS_ACTOR=chaos-drill",
            IMAGE,
        )  # fmt: skip
        cp = f"http://127.0.0.1:{cp_port}"
        assert _until(lambda: _get(cp + "/readyz") == 200, timeout=120), _logs(names["cp"])
        yield {"cp": cp, "token": token, "names": names}
    finally:
        for name in names.values():
            _run("docker", "rm", "-f", name, check=False)
        _run("docker", "network", "rm", net, check=False)
        if TIMINGS:
            print("\nbackbone drill:", json.dumps(TIMINGS, indent=1))


def _health(stack) -> dict:
    return httpx.get(stack["cp"] + "/health", timeout=10).json()


def _get(url: str, timeout: float = 5.0) -> int:
    try:
        return httpx.get(url, timeout=timeout).status_code
    except httpx.HTTPError:
        return 0


def _logs(name: str) -> str:
    return _run("docker", "logs", "--tail", "80", name, check=False).stderr[-4000:]


def _metric(stack, name: str) -> float:
    """One gauge from the control plane's /metrics, or -1 when it is absent."""
    body = httpx.get(stack["cp"] + "/metrics", timeout=10).text
    found = re.search(rf"^{re.escape(name)}(?:\{{[^}}]*\}})? ([0-9.e+-]+)$", body, re.M)
    return float(found.group(1)) if found else -1.0


def _retrain(stack, key: str, model: str = "JPCP", dataset: str = "PM100Dataset") -> int:
    try:
        return httpx.post(
            stack["cp"] + "/v1/retrain",
            json={"model_name": model, "dataset_name": dataset},
            headers={"Authorization": f"Bearer {stack['token']}", "Idempotency-Key": key},
            timeout=15,
        ).status_code
    except httpx.HTTPError:
        return 0


def _outbox_rows(stack, published: bool = True) -> int:
    """Outbox rows the platform recorded, counted in the store itself."""
    where = "published_at IS NOT NULL" if published else "published_at IS NULL"
    out = _run(
        "docker", "exec", stack["names"]["pg"], "psql", "-U", "examlops", "-d", "examlops",
        "-tAc", f"select count(*) from event_outbox where {where}",
    )  # fmt: skip
    return int(out.stdout.strip().splitlines()[-1])


def _stream_messages(stack) -> int:
    """Messages JetStream holds for the platform's stream, counted through the broker itself."""
    out = _run(
        "docker", "exec", stack["names"]["cp"], "python", "-c",
        "import asyncio, json, nats\n"
        "async def main():\n"
        "    nc = await nats.connect('nats://nats:4222')\n"
        "    js = nc.jetstream()\n"
        "    total = 0\n"
        "    for name in await js.streams_info():\n"
        "        total += name.state.messages\n"
        "    print(json.dumps({'messages': total}))\n"
        "    await nc.close()\n"
        "asyncio.run(main())",
        check=False,
    )  # fmt: skip
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    return int(json.loads(line[-1])["messages"]) if line else -1


# ── the drill, in order ───────────────────────────────────────────────────────


def test_1_with_the_bus_up_events_reach_it(stack):
    assert _retrain(stack, "bus-before") == 202
    assert _until(lambda: _stream_messages(stack) > 0, timeout=60), "no event reached JetStream"
    TIMINGS["messages_before_outage"] = _stream_messages(stack)
    assert _until(lambda: _metric(stack, "examlops_event_outbox_pending") == 0, timeout=60), (
        "the outbox never drained with the bus up"
    )


def test_2_with_the_bus_gone_work_is_still_accepted(stack):
    _run("docker", "kill", stack["names"]["nats"])
    # Distinct model/dataset pairs: the same retrain twice in flight is refused with 409 by
    # design, which would say nothing about the bus.
    accepted = [
        _retrain(stack, f"bus-outage-{i}", model=model, dataset=dataset)
        for i, (model, dataset) in enumerate(PAIRS)
    ]
    TIMINGS["retrain_statuses_during_outage"] = accepted
    assert accepted == [202] * len(PAIRS), "the platform refused work because the bus was down"

    # The backlog is visible, and the API stays in rotation: publishing is asynchronous.
    assert _until(lambda: _metric(stack, "examlops_event_outbox_pending") > 0, timeout=30), (
        "the outbox backlog is invisible while the bus is down"
    )
    TIMINGS["outbox_pending_during_outage"] = _metric(stack, "examlops_event_outbox_pending")
    # The outage is seen and reported: a publisher that is merely constructed proves nothing, so
    # the startup check asks the broker.
    assert _until(
        lambda: str(_health(stack)["startup_checks"].get("event_publisher", "")).startswith("fail"),
        timeout=60,
    ), "the control plane never noticed the broker was gone"
    body = _health(stack)
    TIMINGS["health_status_during_outage"] = body["status"]
    assert body["status"] == "degraded"
    assert body["runtime"]["event_relay_error"], "the relay's failure is not visible in /health"
    readiness = [_get(stack["cp"] + "/readyz") for _ in range(3)]
    TIMINGS["readyz_during_outage"] = readiness
    assert readiness == [200, 200, 200], (
        "a bus outage took the control plane out of rotation; publishing is asynchronous, so an "
        "unreachable broker must not make a working API unavailable"
    )


def test_3_when_the_bus_returns_the_backlog_drains_once(stack):
    before = TIMINGS["messages_before_outage"]
    _run("docker", "start", stack["names"]["nats"])
    started = time.monotonic()
    assert _until(lambda: _metric(stack, "examlops_event_outbox_pending") == 0, timeout=120), (
        f"the outbox never drained: {_metric(stack, 'examlops_event_outbox_pending')} pending"
    )
    TIMINGS["seconds_to_drain_after_recovery"] = round(time.monotonic() - started, 2)
    messages = _stream_messages(stack)
    published, unpublished = _outbox_rows(stack), _outbox_rows(stack, published=False)
    TIMINGS["messages_after_recovery"] = messages
    TIMINGS["outbox_rows_published"] = published
    assert isinstance(before, int)
    # Exactly once, measured rather than assumed: every row the platform marked published is one
    # message in the stream, and none is there twice. (JetStream dedupes on Nats-Msg-Id, so even a
    # relay that re-sent a row it had already delivered would not double it.)
    assert unpublished == 0, f"{unpublished} events never left the outbox"
    assert messages == published, f"{messages} messages for {published} published rows"
    # And the outage did not stop the platform from producing events.
    assert messages > before, (messages, before)
    assert _get(stack["cp"] + "/readyz") == 200
    # The publisher check clears itself once the broker is back, with no restart.
    assert _until(
        lambda: _health(stack)["startup_checks"].get("event_publisher") == "ok", timeout=60
    ), "the publisher check never recovered"


# ── both dependencies at once, and a partial recovery ─────────────────────────


def test_4_with_the_store_and_the_bus_both_gone_the_platform_refuses_cleanly(stack):
    """Losing everything at once must still be a clean refusal, not a hang or a crash."""
    _run("docker", "kill", stack["names"]["nats"])
    _run("docker", "kill", stack["names"]["pg"])
    killed = time.monotonic()
    assert _until(lambda: _get(stack["cp"] + "/readyz") == 503, timeout=30), (
        "the control plane still reported ready without its store"
    )
    TIMINGS["combined_seconds_to_unready"] = round(time.monotonic() - killed, 2)
    # The store is what a write needs, so now the retrain *is* refused — and says so.
    statuses = {_retrain(stack, f"both-gone-{i}", *pair) for i, pair in enumerate(PAIRS)}
    TIMINGS["combined_retrain_statuses"] = sorted(statuses)
    assert statuses <= {503}, statuses
    assert _get(stack["cp"] + "/livez") == 200, (
        "liveness failed, so an orchestrator would restart it"
    )


def test_5_the_store_alone_coming_back_is_enough_to_serve_again(stack):
    """Partial recovery is the common case: one dependency returns before the other. The platform
    must come back for what it can do, and keep queueing what it cannot."""
    _run("docker", "start", stack["names"]["pg"])
    started = time.monotonic()
    # Postgres replays its write-ahead log after a SIGKILL before it accepts connections; measured
    # apart from the platform's own recovery, which is what this drill is about.
    assert _until(
        lambda: _run("docker", "exec", stack["names"]["pg"], "pg_isready", "-U", "examlops",
                     check=False).returncode == 0,
        timeout=120,
    ), "postgres did not come back"  # fmt: skip
    accepting = time.monotonic()
    TIMINGS["combined_seconds_postgres_took"] = round(accepting - started, 2)
    assert _until(lambda: _get(stack["cp"] + "/readyz") == 200, timeout=120), _logs(
        stack["names"]["cp"]
    )
    TIMINGS["combined_seconds_control_plane_took_after_that"] = round(
        time.monotonic() - accepting, 2
    )

    accepted = [
        _retrain(stack, f"store-back-{i}", model=model, dataset=dataset)
        for i, (model, dataset) in enumerate(PAIRS)
    ]
    TIMINGS["retrain_statuses_with_bus_still_down"] = accepted
    assert accepted == [202] * len(PAIRS), "work was refused although the store was back"
    assert _until(lambda: _metric(stack, "examlops_event_outbox_pending") > 0, timeout=30), (
        "nothing queued while the broker was still gone"
    )
    # Degraded again once the relay has tried and failed against the still-dead broker: the relay
    # runs every second, but a cycle that is mid-connect has not failed yet.
    # The outage must be *visible*, and quickly. A relay cycle against a dead broker used to cost
    # one client timeout per queued event — 36 s for these six, over eight minutes at the default
    # batch — and `/health` reports the last completed cycle, so it said `ok` for all of it. The
    # batch now stops at the first unreachable answer, which is one timeout.
    queued = time.monotonic()
    assert _until(lambda: _health(stack)["status"] == "degraded", timeout=30), _health(stack)
    TIMINGS["combined_seconds_until_the_outage_was_visible"] = round(time.monotonic() - queued, 2)
    body = _health(stack)
    assert "unavailable" in str(body["runtime"]["event_relay_error"]), body["runtime"]
    assert body["ready"] is True, "the bus being down took the replica out of rotation"
    # And the wait did not consume the events' retry budget: they are pending, not poison.
    assert body["runtime"]["outbox"]["poison"] == 0, body["runtime"]["outbox"]


def test_6_when_the_bus_follows_the_backlog_drains_exactly_once(stack):
    _run("docker", "start", stack["names"]["nats"])
    started = time.monotonic()
    assert _until(lambda: _metric(stack, "examlops_event_outbox_pending") == 0, timeout=180), (
        f"the outbox never drained: {_metric(stack, 'examlops_event_outbox_pending')} pending"
    )
    TIMINGS["combined_seconds_to_drain"] = round(time.monotonic() - started, 2)
    published, unpublished = _outbox_rows(stack), _outbox_rows(stack, published=False)
    messages = _stream_messages(stack)
    TIMINGS["combined_messages"] = messages
    TIMINGS["combined_published_rows"] = published
    assert unpublished == 0
    assert messages == published, f"{messages} messages for {published} published rows"
    assert _get(stack["cp"] + "/readyz") == 200
