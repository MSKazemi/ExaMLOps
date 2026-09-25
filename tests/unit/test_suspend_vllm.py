"""ADR 0109 decision 5 — the engine-delegated ``vllm-sleep`` backend.

A faithful fake of vLLM's sleep-mode HTTP surface (``POST /sleep?level=N``, ``POST /wake_up``,
``GET /is_sleeping``, ``GET /health``) runs behind ``httpx.MockTransport``, as a small state
machine: it only answers the routes vLLM serves when started with ``--enable-sleep-mode`` and
``VLLM_SERVER_DEV_MODE=1`` (404 otherwise), and it keeps the ``is_sleeping`` state the real engine
reports. No GPU and no network.
"""

from __future__ import annotations

import httpx
import pytest

from examlops import platform_db
from examlops.suspend import STATE_SERVING_REPLICA, preemption_promise, service
from examlops.suspend.engine import URL_ENV, VLLMSleepBackend
from examlops.suspend.types import SuspendError, SuspendUnsupported

URL = "http://gpu-03:8000"


class FakeVLLM:
    def __init__(self, *, dev_mode: bool = True, ignore_sleep: bool = False) -> None:
        self.sleeping = False
        self.dev_mode = dev_mode
        self.ignore_sleep = ignore_sleep
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path, dict(request.url.params)))
        self.headers.append(request.headers)
        if path == "/health" and method == "GET":
            return httpx.Response(200)
        if not self.dev_mode:
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/is_sleeping" and method == "GET":
            return httpx.Response(200, json={"is_sleeping": self.sleeping})
        if path == "/sleep" and method == "POST":
            if not self.ignore_sleep:
                self.sleeping = True
            return httpx.Response(200)
        if path == "/wake_up" and method == "POST":
            self.sleeping = False
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv("EXAMLOPS_VLLM_API_KEY", raising=False)
    platform_db.init_db()


def _backend(fake: FakeVLLM) -> VLLMSleepBackend:
    return VLLMSleepBackend(transport=httpx.MockTransport(fake))


def _snap(b: VLLMSleepBackend, **opts):
    return b.snapshot(STATE_SERVING_REPLICA, "llm-a", options={"base_url": URL, **opts})


def test_capability_is_narrow_and_cannot_back_preemption():
    cap = VLLMSleepBackend().capability()
    assert cap.tiers == ("local_memory",) and cap.gpu_state is False
    assert cap.communicator_rebuild_applicable is False and cap.basis == "unknown"
    promise = preemption_promise(cap)
    assert promise.can_promise is False
    assert any("survives loss of the compute" in r for r in promise.reasons)


def test_sleep_then_wake_round_trip_reports_the_split():
    fake = FakeVLLM()
    b = _backend(fake)
    h = _snap(b)
    assert fake.sleeping is True
    assert ("POST", "/sleep", {"level": "1"}) in fake.calls
    assert h.pointer == {"base_url": URL, "level": 1} and h.state_bytes is None
    rep = b.restore(h)
    assert rep.restored and fake.sleeping is False
    assert rep.state_transfer_s is not None and rep.communicator_rebuild_s == 0.0
    assert fake.calls[-1][:2] == ("GET", "/health")


def test_already_asleep_is_refused_not_double_recorded():
    fake = FakeVLLM()
    fake.sleeping = True
    with pytest.raises(SuspendError, match="already asleep"):
        _snap(_backend(fake))
    assert not any(c[1] == "/sleep" for c in fake.calls)


def test_server_without_sleep_mode_is_unsupported():
    with pytest.raises(SuspendUnsupported, match="enable-sleep-mode"):
        _snap(_backend(FakeVLLM(dev_mode=False)))


def test_sleep_that_did_not_take_effect_is_an_error():
    with pytest.raises(SuspendError, match="not sleeping"):
        _snap(_backend(FakeVLLM(ignore_sleep=True)))


def test_level_two_is_refused():
    fake = FakeVLLM()
    with pytest.raises(SuspendUnsupported, match="level 2"):
        _snap(_backend(fake), level=2)
    assert fake.calls == []


@pytest.mark.parametrize("bad", ["", "file:///etc/passwd", "gpu-03:8000", "ftp://x"])
def test_address_must_be_http(bad):
    with pytest.raises(SuspendError):
        VLLMSleepBackend(transport=httpx.MockTransport(FakeVLLM())).snapshot(
            STATE_SERVING_REPLICA, "llm-a", options={"base_url": bad}
        )


def test_address_can_come_from_env(monkeypatch):
    monkeypatch.setenv(URL_ENV, URL)
    fake = FakeVLLM()
    h = _backend(fake).snapshot(STATE_SERVING_REPLICA, "llm-a")
    assert h.pointer["base_url"] == URL and fake.sleeping


def test_bearer_key_is_sent_only_when_configured(monkeypatch):
    fake = FakeVLLM()
    _snap(_backend(fake))
    assert "authorization" not in fake.headers[0]
    monkeypatch.setenv("EXAMLOPS_VLLM_API_KEY", "k-123")
    monkeypatch.setenv(URL_ENV, URL)
    fake2 = FakeVLLM()
    _snap(_backend(fake2))
    assert fake2.headers[0]["authorization"] == "Bearer k-123"


def test_bearer_key_is_origin_bound_to_the_configured_server(monkeypatch):
    """A per-call --base-url elsewhere must not receive the platform's vLLM key."""
    monkeypatch.setenv("EXAMLOPS_VLLM_API_KEY", "k-123")
    other = {"base_url": "http://attacker:8000"}
    fake = FakeVLLM()
    _backend(fake).snapshot(STATE_SERVING_REPLICA, "llm-a", options=other)
    assert all("authorization" not in h for h in fake.headers)  # no configured origin at all
    monkeypatch.setenv(URL_ENV, URL)
    fake2 = FakeVLLM()
    _backend(fake2).snapshot(STATE_SERVING_REPLICA, "llm-a", options=other)
    assert all("authorization" not in h for h in fake2.headers)
    fake3 = FakeVLLM()
    same = {"base_url": "http://GPU-03:8000/"}
    _backend(fake3).snapshot(STATE_SERVING_REPLICA, "llm-a", options=same)
    assert all(h["authorization"] == "Bearer k-123" for h in fake3.headers)


def test_a_non_integer_level_is_a_suspend_error_not_a_crash():
    with pytest.raises(SuspendError, match="integer"):
        _snap(_backend(FakeVLLM()), level="deep")


def test_restore_of_an_engine_that_was_restarted_fails_honestly():
    fake = FakeVLLM()
    b = _backend(fake)
    h = _snap(b)
    fake.sleeping = False  # restarted / woken elsewhere
    rep = b.restore(h)
    assert rep.restored is False and "no longer exists" in rep.detail


def test_unreachable_engine_is_a_retryable_suspend_error_not_a_failed_restore():
    """Unreachable says nothing about the engine's state, so it is not a verdict on the restore."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake = FakeVLLM()
    h = _snap(_backend(fake))
    with pytest.raises(SuspendError, match="refused"):
        VLLMSleepBackend(transport=httpx.MockTransport(boom)).restore(h)
    assert fake.sleeping is True


def test_wrong_subject_kind_and_gpu_state_requests_are_refused():
    b = _backend(FakeVLLM())
    with pytest.raises(SuspendUnsupported):
        b.snapshot("training_run_checkpoint", "x", options={"base_url": URL})
    with pytest.raises(SuspendUnsupported, match="GPU state"):
        _snap(b, require_gpu_state=True)


def test_through_the_service_is_audited(monkeypatch):
    fake = FakeVLLM()
    monkeypatch.setattr(
        "examlops.suspend.engine.VLLMSleepBackend.__init__",
        lambda self, transport=None: setattr(self, "_transport", httpx.MockTransport(fake)),
    )
    h = service.suspend(
        "llm-a",
        subject_kind=STATE_SERVING_REPLICA,
        backend="vllm-sleep",
        options={"base_url": URL},
        actor="op",
    )
    assert service.resume(h.snapshot_id, actor="op").restored
    with platform_db.get_db() as c:
        actions = [r[0] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]
    assert actions[-2:] == ["suspend_snapshot", "suspend_resume"]
    row = service.status(h.snapshot_id)
    assert row is not None and row["status"] == "resumed"


def test_an_unreachable_engine_leaves_the_record_suspended_and_retryable(monkeypatch):
    """A transport error on resume says nothing about the engine; it must not mark it failed.

    Marking it ``failed`` would strand a sleeping engine: resume refuses anything not suspended.
    """
    fake = FakeVLLM()
    net = {"up": True}

    def route(request: httpx.Request) -> httpx.Response:
        if not net["up"]:
            raise httpx.ConnectError("connection refused", request=request)
        return fake(request)

    monkeypatch.setattr(
        "examlops.suspend.engine.VLLMSleepBackend.__init__",
        lambda self, transport=None: setattr(self, "_transport", httpx.MockTransport(route)),
    )
    h = service.suspend(
        "llm-a", subject_kind=STATE_SERVING_REPLICA, backend="vllm-sleep", options={"base_url": URL}
    )
    assert fake.sleeping is True
    net["up"] = False
    with pytest.raises(SuspendError, match="connection refused"):
        service.resume(h.snapshot_id)
    row = service.status(h.snapshot_id)
    assert row is not None and row["status"] == "suspended"
    with platform_db.get_db() as c:
        actions = [r[0] for r in c.execute("SELECT action FROM audit_events ORDER BY id")]
    assert actions[-1] == "suspend_resume_error"
    net["up"] = True  # the engine is reachable again: the retry wakes it
    assert service.resume(h.snapshot_id).restored is True
    assert fake.sleeping is False
    row = service.status(h.snapshot_id)
    assert row is not None and row["status"] == "resumed"


def test_a_5xx_on_resume_is_transient_not_a_failed_restore():
    fake = FakeVLLM()
    b = _backend(fake)
    h = b.snapshot(STATE_SERVING_REPLICA, "llm-a", options={"base_url": URL})
    b._transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(SuspendError, match="503"):
        b.restore(h)
