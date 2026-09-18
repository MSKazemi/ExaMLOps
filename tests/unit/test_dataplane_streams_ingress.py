"""Unit tests for the stream ingress orchestration and its metrics (ADR 0130/0131, Plan 2, A5).

Covers: the ok path (telemetry record with embedding *stats*, never the raw vector; drift fed a
success), reply-first ordering (the reply callback runs before the telemetry offer, and a drift
trigger blocked on an Event cannot hold ``handle()`` up), outcomes that must never feed drift,
payload-free validation errors, in-flight and rate shedding, the coordinator staying off the
default hot path, fail-open on a coordinator error, E18 replays on a real ``DbCoordinator``, the
budget folded into the request and sent as a header, and the ``dataplane_stream_*`` metrics
including the spool hooks and the baseline callback.

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``; metric
assertions use a fresh stream name per test and compare deltas, so test order never matters.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY

from examlops.coordination import DbCoordinator
from examlops.data import init_db
from examlops.data.drift import set_drift_auto_retrain
from examlops.dataplane.streams import metrics
from examlops.dataplane.streams.client import BUDGET_HEADER, RayPipelineClient
from examlops.dataplane.streams.drift import BoundedExecutor, DriftAggregator
from examlops.dataplane.streams.ingress import IngressResult, StreamIngress
from examlops.dataplane.streams.schema import ModelSchemaRegistry
from examlops.dataplane.streams.telemetry import TelemetryRecord
from examlops.dataplane.streams.types import (
    InferenceResult,
    StreamBinding,
    StreamLimits,
    StreamRequest,
)

# ── fakes ────────────────────────────────────────────────────────────────────


class _Client:
    def __init__(self, result: InferenceResult | None = None) -> None:
        self.result = result or InferenceResult(
            outcome="ok", prediction=2.5, body={"prediction": 2.5, "model_version": "7"}
        )
        self.calls: list[tuple[StreamRequest, dict[str, Any]]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.raise_exc: Exception | None = None

    def infer(self, req: StreamRequest, body: dict[str, Any]) -> InferenceResult:
        self.calls.append((req, body))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class _Spool:
    def __init__(self, log: list[str] | None = None, accept: bool = True) -> None:
        self.records: list[TelemetryRecord] = []
        self.log = log
        self.accept = accept

    def offer(self, record: TelemetryRecord) -> bool:
        if self.log is not None:
            self.log.append("offer")
        self.records.append(record)
        return self.accept


class _Drift:
    def __init__(self, log: list[str] | None = None) -> None:
        self.observed: list[tuple[str, bool, dict[str, Any]]] = []
        self.log = log

    def observe(self, model: str, failed: bool, **kw: Any) -> None:
        if self.log is not None:
            self.log.append("drift")
        self.observed.append((model, failed, kw))


class _Coord:
    """Counts calls; ``fail`` makes every call raise; ``allow_n`` caps allow() answers."""

    def __init__(self, *, fail: bool = False, allow_n: int = 10**9) -> None:
        self.fail = fail
        self.allow_n = allow_n
        self.calls: list[str] = []
        self.seen: set[str] = set()

    def try_lock(self, key: str, holder: str, ttl_s: float) -> bool:
        return True

    def unlock(self, key: str, holder: str) -> None:
        return None

    def first_seen(self, key: str, ttl_s: float) -> bool:
        self.calls.append(f"first_seen:{key}")
        if self.fail:
            raise RuntimeError("coordinator down")
        new = key not in self.seen
        self.seen.add(key)
        return new

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        self.calls.append(f"allow:{bucket}")
        if self.fail:
            raise RuntimeError("coordinator down")
        self.allow_n -= 1
        return self.allow_n >= 0


def _binding(stream: str | None = None, **overrides: Any) -> StreamBinding:
    fields: dict[str, Any] = {
        "project": "proj",
        "name": stream or f"s-{uuid.uuid4().hex[:8]}",
        "connector": "http",
        "model": "JPCP",
        "alias": "Production",
        "address": "/push/x",
        "connection": None,
    }
    fields.update(overrides)
    return StreamBinding(**fields)


def _req(binding: StreamBinding, **overrides: Any) -> StreamRequest:
    fields: dict[str, Any] = {
        "stream": binding.name,
        "model": binding.model,
        "alias": binding.alias,
        "payload": {"embedding": [3.0, 4.0], "num_nodes": 2},
    }
    fields.update(overrides)
    return StreamRequest(**fields)


@pytest.fixture
def registry(tmp_path: Path) -> ModelSchemaRegistry:
    """A registry over an empty models dir: no schema, so the payload is forwarded as-is."""
    return ModelSchemaRegistry(tmp_path)


def _ingress(
    registry: ModelSchemaRegistry,
    client: Any = None,
    spool: Any = None,
    drift: Any = None,
    coord: Any = None,
) -> StreamIngress:
    return StreamIngress(
        client or _Client(), spool or _Spool(), drift, registry, coord if coord else _Coord()
    )


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# ── the ok path ──────────────────────────────────────────────────────────────


def test_ok_offers_telemetry_with_embedding_stats_and_feeds_a_success(
    registry: ModelSchemaRegistry,
) -> None:
    spool, drift = _Spool(), _Drift()
    binding = _binding()
    result = _ingress(registry, spool=spool, drift=drift).handle(
        binding, _req(binding, metadata={"job_id": "j-1", "tenant": "t1"})
    )
    assert result.outcome == "ok" and result.status == 200 and result.prediction == 2.5
    assert result.replayed is False
    (record,) = spool.records
    assert record.outcome == "ok" and record.prediction == 2.5
    assert record.model == "JPCP" and record.stream == binding.name and record.project == "proj"
    assert record.model_version == "7" and record.job_id == "j-1"
    assert record.embedding is not None
    assert record.embedding.norm == pytest.approx(5.0) and record.embedding.dim == 2
    assert "[3.0, 4.0]" not in repr(record)  # a summary, never the raw vector
    assert drift.observed == [
        ("JPCP", False, {"stream": binding.name, "connector": "http"}),
    ]


def test_an_embedding_under_features_is_summarised_too(registry: ModelSchemaRegistry) -> None:
    spool = _Spool()
    binding = _binding()
    _ingress(registry, spool=spool).handle(
        binding, _req(binding, payload={"features": {"embedding": [1.0, 1.0]}})
    )
    assert spool.records[0].embedding is not None
    assert spool.records[0].embedding.mean == pytest.approx(1.0)


def test_ingress_result_is_an_inference_result(registry: ModelSchemaRegistry) -> None:
    binding = _binding()
    result = _ingress(registry).handle(binding, _req(binding))
    assert isinstance(result, IngressResult)
    assert isinstance(result, InferenceResult)


# ── reply first ──────────────────────────────────────────────────────────────


def test_the_reply_is_sent_before_the_telemetry_offer(registry: ModelSchemaRegistry) -> None:
    log: list[str] = []
    replies: list[IngressResult] = []

    def reply(result: IngressResult) -> None:
        log.append("reply")
        replies.append(result)

    binding = _binding()
    result = _ingress(registry, spool=_Spool(log), drift=_Drift(log)).handle(
        binding, _req(binding), reply=reply
    )
    assert log == ["reply", "offer", "drift"]
    assert replies == [result]


def test_a_blocked_drift_trigger_never_holds_the_reply(registry: ModelSchemaRegistry) -> None:
    init_db()
    set_drift_auto_retrain("JPCP", enabled=True, dataset_name="DatasetX")
    started, release = threading.Event(), threading.Event()
    submitted: list[str] = []

    class _BlockingTrigger:
        def submit(self, model: str, dataset: str, backend: str, reason: str, **kw: Any) -> bool:
            started.set()
            release.wait(timeout=10)
            submitted.append(model)
            return True

    drift = DriftAggregator(_BlockingTrigger(), _Coord(), 300.0, 1, 1.0, executor=BoundedExecutor())
    client = _Client(InferenceResult(outcome="model", body={"error": "model"}, status=500))
    binding = _binding()
    returned: list[IngressResult] = []
    worker = threading.Thread(
        target=lambda: returned.append(
            _ingress(registry, client=client, drift=drift).handle(binding, _req(binding))
        )
    )
    worker.start()
    worker.join(timeout=10)
    assert started.wait(timeout=10)
    assert not worker.is_alive() and returned[0].outcome == "model"  # returned, trigger blocked
    assert submitted == []
    release.set()
    drift.close(timeout=10)
    assert submitted == ["JPCP"]


def test_a_raising_reply_is_reraised_after_the_bookkeeping(registry: ModelSchemaRegistry) -> None:
    spool = _Spool()

    def reply(result: IngressResult) -> None:
        raise ConnectionError("caller went away")

    binding = _binding()
    with pytest.raises(ConnectionError):
        _ingress(registry, spool=spool).handle(binding, _req(binding), reply=reply)
    assert len(spool.records) == 1


# ── what feeds drift ─────────────────────────────────────────────────────────


def test_a_model_failure_feeds_drift(registry: ModelSchemaRegistry) -> None:
    drift = _Drift()
    client = _Client(InferenceResult(outcome="model", body={"error": "model"}, status=500))
    binding = _binding()
    _ingress(registry, client=client, drift=drift).handle(binding, _req(binding))
    assert [(m, f) for m, f, _ in drift.observed] == [("JPCP", True)]


def test_a_served_answer_without_a_prediction_is_a_failure(registry: ModelSchemaRegistry) -> None:
    drift = _Drift()
    client = _Client(InferenceResult(outcome="ok", prediction=None, body={}))
    binding = _binding()
    _ingress(registry, client=client, drift=drift).handle(binding, _req(binding))
    assert [f for _, f, _ in drift.observed] == [True]


@pytest.mark.parametrize(
    "outcome", ["transport", "overloaded", "deadline", "not_found", "unexpected", "validation"]
)
def test_non_model_outcomes_never_feed_drift(registry: ModelSchemaRegistry, outcome: Any) -> None:
    drift, spool = _Drift(), _Spool()
    client = _Client(InferenceResult(outcome=outcome, body={"error": outcome}, status=502))
    binding = _binding()
    result = _ingress(registry, client=client, spool=spool, drift=drift).handle(
        binding, _req(binding)
    )
    assert result.outcome == outcome
    assert drift.observed == []
    assert spool.records == []  # M8: telemetry is offered for ok only


def test_a_model_failure_feeds_drift_but_offers_no_telemetry(
    registry: ModelSchemaRegistry,
) -> None:
    """M8 (bridge parity R2): the bridge offered telemetry from its success path only."""
    drift, spool = _Drift(), _Spool()
    client = _Client(InferenceResult(outcome="model", body={"error": "model"}, status=500))
    binding = _binding()
    _ingress(registry, client=client, spool=spool, drift=drift).handle(binding, _req(binding))
    assert spool.records == []
    assert [f for _, f, _ in drift.observed] == [True]


def test_a_validation_failure_is_422_never_raised_never_fed(tmp_path: Path) -> None:
    (tmp_path / "jpcp.yaml").write_text(
        "name: JPCP\ninference:\n  input_schema:\n    embedding: list[float]\n    num_nodes: int\n"
    )
    registry = ModelSchemaRegistry(tmp_path)
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding()
    req = _req(binding, payload={"embedding": [1.0], "user_secret": "hunter2-token"})
    result = _ingress(registry, client=client, spool=spool, drift=drift).handle(binding, req)
    assert result.outcome == "validation" and result.status == 422
    assert "num_nodes" in result.body["detail"]
    assert "hunter2-token" not in repr(result)
    assert client.calls == [] and spool.records == [] and drift.observed == []


def test_a_request_for_another_model_is_refused(registry: ModelSchemaRegistry) -> None:
    """M1: the binding is the authorization unit — never infer against the request's model."""
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding()
    labels = {
        "project": "proj",
        "stream": binding.name,
        "connector": "http",
        "model": "JPCP",
        "outcome": "validation",
    }
    before = _sample("dataplane_stream_requests_total", labels)
    result = _ingress(registry, client=client, spool=spool, drift=drift).handle(
        binding, _req(binding, model="SOMEONE_ELSES_MODEL")
    )
    assert result.outcome == "validation" and result.status == 422
    assert result.body["detail"] == "this stream is bound to model JPCP"
    assert client.calls == [] and spool.records == [] and drift.observed == []
    assert _sample("dataplane_stream_requests_total", labels) - before == 1  # binding's label


def test_a_non_finite_payload_is_a_validation_result(registry: ModelSchemaRegistry) -> None:
    binding = _binding()
    result = _ingress(registry).handle(binding, _req(binding, payload={"x": float("nan")}))
    assert result.outcome == "validation" and result.status == 422


def test_a_raising_client_is_an_unexpected_result(registry: ModelSchemaRegistry) -> None:
    client, drift = _Client(), _Drift()
    client.raise_exc = RuntimeError("bug")
    binding = _binding()
    result = _ingress(registry, client=client, drift=drift).handle(binding, _req(binding))
    # 500, not 502 (review I3): `unexpected` means the platform surprised itself, which is our own
    # server error — and the push route and the replay route now answer it the same way.
    assert result.outcome == "unexpected" and result.status == 500
    assert drift.observed == []


# ── the binding's alias is authoritative (C1) ────────────────────────────────


def test_a_request_naming_another_alias_is_refused(registry: ModelSchemaRegistry) -> None:
    """C1: the alias picks the model *version* and the drift window a prediction lands in, so a
    message may not choose it — exactly as it may not choose the model (M1)."""
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding(alias="Canary")
    result = _ingress(registry, client=client, spool=spool, drift=drift).handle(
        binding, _req(binding, alias="Production")
    )
    assert result.outcome == "validation" and result.status == 422
    assert result.body["detail"] == "this stream is bound to alias Canary"
    assert client.calls == [] and spool.records == [] and drift.observed == []


def test_the_bindings_own_alias_and_an_empty_one_are_both_served(
    registry: ModelSchemaRegistry,
) -> None:
    client, spool = _Client(), _Spool()
    binding = _binding(alias="Canary")
    ingress = _ingress(registry, client=client, spool=spool)
    assert ingress.handle(binding, _req(binding, alias="Canary")).outcome == "ok"
    assert ingress.handle(binding, _req(binding, alias="")).outcome == "ok"
    assert [routed.alias for routed, _ in client.calls] == ["Canary", "Canary"]
    assert [r.alias for r in spool.records] == ["Canary", "Canary"]


def test_allow_alias_override_opts_one_stream_in(registry: ModelSchemaRegistry) -> None:
    """The one escape (C1): opt in per binding, and even then only a known alias name."""
    client, spool = _Client(), _Spool()
    binding = _binding(alias="Production", options={"allow_alias_override": True})
    ingress = _ingress(registry, client=client, spool=spool)
    result = ingress.handle(binding, _req(binding, alias="Canary"))
    assert result.outcome == "ok"
    routed, _ = client.calls[0]
    assert routed.alias == "Canary" and spool.records[0].alias == "Canary"
    # an alias outside MLflowAlias is still refused, so the two snapshot tables stay bounded
    refused = ingress.handle(binding, _req(binding, alias="../../etc"))
    assert refused.outcome == "validation" and len(client.calls) == 1


@pytest.mark.parametrize("option", [False, "true", 1, None])
def test_only_a_real_true_opts_in(registry: ModelSchemaRegistry, option: Any) -> None:
    client = _Client()
    binding = _binding(alias="Production", options={"allow_alias_override": option})
    result = _ingress(registry, client=client).handle(binding, _req(binding, alias="Canary"))
    assert result.outcome == "validation" and client.calls == []


def test_a_message_can_never_name_its_own_tenant(registry: ModelSchemaRegistry) -> None:
    """I4: enforced where the value is READ, not only where each connector strips it."""
    spool = _Spool()
    binding = _binding()
    _ingress(registry, spool=spool).handle(
        binding,
        _req(
            binding,
            metadata={"tenant": "evil", "job_id": "j-9"},
            payload={"tenant": "also-evil", "embedding": [1.0, 1.0]},
        ),
    )
    (record,) = spool.records
    assert not hasattr(record, "tenant")  # the field is gone, not merely unset (M7)
    assert "evil" not in repr(record) and record.project == "proj" and record.job_id == "j-9"


@pytest.mark.parametrize("options", [[], "nope", None, 7, ("a",)])
def test_a_binding_with_malformed_options_is_a_validation_result_never_a_raise(
    registry: ModelSchemaRegistry, options: Any
) -> None:
    """Re-review: ``resolve_alias`` reads ``binding.options``, and it used to run OUTSIDE the try
    that makes ``handle()`` non-raising — so a binding whose options are not a mapping raised an
    ``AttributeError`` straight into the connector. The catalog always coerces options to a dict,
    so this is defence in depth for a binding built by hand or by a pack connector."""
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding(options=options)
    result = _ingress(registry, client=client, spool=spool, drift=drift).handle(
        binding, _req(binding)
    )
    assert result.outcome == "validation" and result.status == 422
    assert result.body["detail"] == (
        f"stream {binding.name!r} has malformed options "
        f"({type(options).__name__}, expected a mapping)"
    )
    assert client.calls == [] and spool.records == [] and drift.observed == []


def test_a_malformed_options_value_is_never_echoed(registry: ModelSchemaRegistry) -> None:
    """An option value is connector config and may carry a credential: the detail names the
    stream and the *type*, never the value."""
    binding = _binding(options=["postgres://user:hunter2@db/x"])
    result = _ingress(registry).handle(binding, _req(binding))
    assert result.outcome == "validation" and "hunter2" not in str(result.body)


# ── the model schemas follow the pack (I2) ───────────────────────────────────


def test_a_model_added_after_startup_is_validated_after_a_refresh(tmp_path: Path) -> None:
    """I2: the pack is re-synced every 60 s, so the schema registry must be re-read with it —
    a model whose schema the ingress never re-read would have every message forwarded raw."""
    client = _Client()
    registry = ModelSchemaRegistry(tmp_path)
    binding = _binding(model="LATE")
    ingress = _ingress(registry, client=client)

    # Before: no schema for LATE, so the payload is forwarded unchanged (passthrough branch).
    assert ingress.handle(binding, _req(binding, payload={"junk": 1})).outcome == "ok"
    assert client.calls[0][1] == {"junk": 1}

    (tmp_path / "late.yaml").write_text(
        "name: LATE\ninference:\n  input_schema:\n    num_nodes: int\n"
    )
    ingress.refresh_schema()

    # After: the declared field is extracted, and a payload missing it is a validation failure.
    assert ingress.handle(binding, _req(binding, payload={"num_nodes": 4, "junk": 1})).outcome == (
        "ok"
    )
    assert client.calls[1][1] == {"num_nodes": 4}
    late = ingress.handle(binding, _req(binding, payload={"junk": 1}))
    assert late.outcome == "validation" and "num_nodes" in late.body["detail"]


def test_a_model_whose_yaml_becomes_unreadable_keeps_its_last_good_schema(tmp_path: Path) -> None:
    """Re-review: a refresh may add and change, and it only removes what is genuinely gone. A
    YAML caught mid-write used to un-register its model, dropping a live stream into the
    "forward the payload unchanged" branch — the very failure I2 exists to prevent."""
    client = _Client()
    yaml_path = tmp_path / "late.yaml"
    yaml_path.write_text("name: LATE\ninference:\n  input_schema:\n    num_nodes: int\n")
    registry = ModelSchemaRegistry(tmp_path)
    binding = _binding(model="LATE")
    ingress = _ingress(registry, client=client)
    assert ingress.handle(binding, _req(binding, payload={"junk": 1})).outcome == "validation"

    for broken in ("name: LATE\ninference:\n  input_schema:\n   a: [", "", "   "):
        yaml_path.write_text(broken)  # an editor mid-write / a half-synced pack
        ingress.refresh_schema()
        assert registry.schema_for("LATE") is not None
        late = ingress.handle(binding, _req(binding, payload={"junk": 1}))
        assert late.outcome == "validation" and "num_nodes" in late.body["detail"]
        assert ingress.handle(binding, _req(binding, payload={"num_nodes": 4})).outcome == "ok"

    yaml_path.unlink()  # genuinely gone from the pack: only then is the schema forgotten
    ingress.refresh_schema()
    assert registry.schema_for("LATE") is None
    assert ingress.handle(binding, _req(binding, payload={"junk": 1})).outcome == "ok"


def test_refresh_schema_without_a_registry_resets_the_default(
    registry: ModelSchemaRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "examlops.dataplane.streams.schema.reset_default_registry", lambda: calls.append(1)
    )
    StreamIngress(_Client(), _Spool(), None, None, _Coord()).refresh_schema()
    assert calls == [1]


def test_refresh_schema_never_raises(registry: ModelSchemaRegistry) -> None:
    class _Broken(ModelSchemaRegistry):
        def refresh(self) -> None:
            raise OSError("the pack directory went away")

    ingress = _ingress(_Broken(Path("/nonexistent-pack")))
    ingress.refresh_schema()  # no raise: the previous schemas stay usable


# ── backpressure ─────────────────────────────────────────────────────────────


def test_a_full_semaphore_sheds_as_overloaded(registry: ModelSchemaRegistry) -> None:
    client = _Client()
    client.gate = threading.Event()
    binding = _binding(limits=StreamLimits(max_in_flight=1))
    ingress = _ingress(registry, client=client)
    shed_labels = {"project": "proj", "stream": binding.name, "reason": "in_flight"}
    before = _sample("dataplane_stream_shed_total", shed_labels)
    first = threading.Thread(target=lambda: ingress.handle(binding, _req(binding)))
    first.start()
    assert client.entered.wait(timeout=10)
    assert ingress.stats()[f"proj/{binding.name}"]["in_flight"] == 1
    shed = ingress.handle(binding, _req(binding))
    client.gate.set()
    first.join(timeout=10)
    assert shed.outcome == "overloaded" and shed.status == 503 and shed.retry_after == 1.0
    assert len(client.calls) == 1
    stats = ingress.stats()[f"proj/{binding.name}"]
    assert stats["shed"]["in_flight"] == 1 and stats["in_flight"] == 0
    assert _sample("dataplane_stream_shed_total", shed_labels) - before == 1


def test_a_failing_in_flight_count_never_leaks_the_permit(
    registry: ModelSchemaRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: the permit is taken, then anything between that and the release is inside the try."""
    calls = {"n": 0}
    real_inc = metrics.in_flight_inc

    def flaky_inc(project: str, stream: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("metrics bug")
        real_inc(project, stream)

    monkeypatch.setattr(metrics, "in_flight_inc", flaky_inc)
    binding = _binding(limits=StreamLimits(max_in_flight=1))
    ingress = _ingress(registry)
    first = ingress.handle(binding, _req(binding))
    second = ingress.handle(binding, _req(binding))
    assert first.outcome == "ok"  # a gauge failure is best-effort, never the request's problem
    assert second.outcome == "ok"  # the one permit came back
    assert ingress.stats()[f"proj/{binding.name}"]["in_flight"] == 0


def test_the_rate_limit_sheds(registry: ModelSchemaRegistry) -> None:
    init_db()
    client = _Client()
    binding = _binding(limits=StreamLimits(rate_per_min=2))
    ingress = _ingress(registry, client=client, coord=DbCoordinator())
    outcomes = [ingress.handle(binding, _req(binding)) for _ in range(3)]
    assert [r.outcome for r in outcomes] == ["ok", "ok", "overloaded"]
    assert outcomes[2].retry_after == 30.0  # 60 / rate
    assert len(client.calls) == 2
    assert ingress.stats()[f"proj/{binding.name}"]["shed"]["rate"] == 1


def test_a_slow_rate_caps_retry_after_at_sixty(registry: ModelSchemaRegistry) -> None:
    binding = _binding(limits=StreamLimits(rate_per_min=1))
    ingress = _ingress(registry, coord=_Coord(allow_n=0))
    assert ingress.handle(binding, _req(binding)).retry_after == 60.0


def test_the_default_hot_path_never_touches_the_coordinator(
    registry: ModelSchemaRegistry,
) -> None:
    coord = _Coord()
    binding = _binding()  # rate_per_min=0, no idempotency key
    _ingress(registry, coord=coord).handle(binding, _req(binding))
    assert coord.calls == []


def test_a_coordinator_error_fails_open_and_warns_once(
    registry: ModelSchemaRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    binding = _binding(limits=StreamLimits(rate_per_min=5))
    spool = _Spool()
    ingress = _ingress(registry, spool=spool, coord=_Coord(fail=True))
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.ingress"):
        results = [ingress.handle(binding, _req(binding, idempotency_key="k1")) for _ in range(2)]
    assert [r.outcome for r in results] == ["ok", "ok"]
    assert [r.replayed for r in results] == [False, False]  # unanswered = first-seen
    assert len(spool.records) == 2
    # M6: once per check across both requests — the rate warning never hides the idempotency one
    assert caplog.text.count("rate check failed") == 1
    assert caplog.text.count("idempotency check failed") == 1


# ── idempotency (E18) ────────────────────────────────────────────────────────


def test_a_replayed_idempotency_key_sends_no_telemetry(registry: ModelSchemaRegistry) -> None:
    init_db()
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding()
    ingress = _ingress(registry, client=client, spool=spool, drift=drift, coord=DbCoordinator())
    first = ingress.handle(binding, _req(binding, idempotency_key="abc"))
    again = ingress.handle(binding, _req(binding, idempotency_key="abc"))
    assert (first.replayed, again.replayed) == (False, True)
    assert again.outcome == "ok" and again.prediction == 2.5  # served normally
    assert len(client.calls) == 2
    assert len(spool.records) == 1 and len(drift.observed) == 1
    assert ingress.stats()[f"proj/{binding.name}"]["replayed"] == 1


def test_idempotency_keys_are_scoped_by_project(registry: ModelSchemaRegistry) -> None:
    coord = _Coord()
    ingress = _ingress(registry, coord=coord)
    a = _binding("shared", project="alpha")
    b = _binding("shared", project="beta")
    ra = ingress.handle(a, _req(a, idempotency_key="k"))
    rb = ingress.handle(b, _req(b, idempotency_key="k"))
    assert (ra.replayed, rb.replayed) == (False, False)
    assert "first_seen:dataplane:stream:alpha:shared:k" in coord.calls


def test_a_raise_after_the_permit_is_taken_still_releases_it(
    registry: ModelSchemaRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: the permit is released even when the in-flight bookkeeping itself raises."""
    binding = _binding(limits=StreamLimits(max_in_flight=1))
    ingress = _ingress(registry)
    real = ingress._in_flight
    calls = {"n": 0}

    def exploding(b: StreamBinding, key: str, delta: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("bookkeeping bug")
        real(b, key, delta)

    monkeypatch.setattr(ingress, "_in_flight", exploding)
    first = ingress.handle(binding, _req(binding))
    second = ingress.handle(binding, _req(binding))
    assert first.outcome == "unexpected"  # caught by handle(), never raised
    assert second.outcome == "ok"  # the one permit came back


@pytest.mark.parametrize("first_outcome", ["transport", "deadline", "overloaded", "unexpected"])
def test_a_failed_first_attempt_does_not_consume_its_key(
    registry: ModelSchemaRegistry, first_outcome: Any
) -> None:
    """M2: only a counted (ok/model) outcome consumes the key, so the retry is counted normally."""
    init_db()
    client, spool, drift = _Client(), _Spool(), _Drift()
    binding = _binding()
    ingress = _ingress(registry, client=client, spool=spool, drift=drift, coord=DbCoordinator())
    ok = client.result
    client.result = InferenceResult(
        outcome=first_outcome, body={"error": first_outcome}, status=502
    )
    failed = ingress.handle(binding, _req(binding, idempotency_key="retry-me"))
    client.result = ok
    retried = ingress.handle(binding, _req(binding, idempotency_key="retry-me"))
    assert (failed.replayed, retried.replayed) == (False, False)
    assert retried.outcome == "ok"
    assert len(spool.records) == 1 and len(drift.observed) == 1


def test_a_model_outcome_consumes_the_key(registry: ModelSchemaRegistry) -> None:
    coord = _Coord()
    client = _Client(InferenceResult(outcome="model", body={"error": "model"}, status=500))
    binding = _binding()
    drift = _Drift()
    ingress = _ingress(registry, client=client, drift=drift, coord=coord)
    first = ingress.handle(binding, _req(binding, idempotency_key="k"))
    again = ingress.handle(binding, _req(binding, idempotency_key="k"))
    assert (first.replayed, again.replayed) == (False, True)
    assert len(drift.observed) == 1  # the replayed model failure is not counted twice


def test_the_idempotency_check_runs_after_inference(registry: ModelSchemaRegistry) -> None:
    order: list[str] = []

    class _OrderedCoord(_Coord):
        def first_seen(self, key: str, ttl_s: float) -> bool:
            order.append("first_seen")
            return super().first_seen(key, ttl_s)

    class _OrderedClient(_Client):
        def infer(self, req: StreamRequest, body: dict[str, Any]) -> InferenceResult:
            order.append("infer")
            return super().infer(req, body)

    binding = _binding()
    _ingress(registry, client=_OrderedClient(), coord=_OrderedCoord()).handle(
        binding, _req(binding, idempotency_key="k")
    )
    assert order == ["infer", "first_seen"]


def test_a_shed_request_does_not_consume_its_key(registry: ModelSchemaRegistry) -> None:
    coord = _Coord(allow_n=0)
    binding = _binding(limits=StreamLimits(rate_per_min=1))
    _ingress(registry, coord=coord).handle(binding, _req(binding, idempotency_key="k"))
    assert not any(c.startswith("first_seen") for c in coord.calls)


# ── the budget ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("limit_ms", "caller_ms", "expected"),
    [(500, 200, "200"), (500, None, "500"), (None, 300, "300"), (None, None, None)],
)
def test_the_budget_header_is_the_min_of_binding_and_caller(
    registry: ModelSchemaRegistry, limit_ms: int | None, caller_ms: int | None, expected: Any
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"prediction": 1.0})

    client = RayPipelineClient("http://pipeline.test", transport=httpx.MockTransport(handler))
    binding = _binding(limits=StreamLimits(deadline_ms=limit_ms))
    result = _ingress(registry, client=client).handle(binding, _req(binding, deadline_ms=caller_ms))
    assert result.outcome == "ok"
    assert seen[0].headers.get(BUDGET_HEADER) == expected


def test_the_request_is_routed_with_the_bindings_model_when_unset(
    registry: ModelSchemaRegistry,
) -> None:
    client = _Client()
    binding = _binding()
    _ingress(registry, client=client).handle(binding, _req(binding, model="", alias=""))
    routed, _ = client.calls[0]
    assert (routed.model, routed.alias) == ("JPCP", "Production")


# ── metrics ──────────────────────────────────────────────────────────────────


def test_requests_are_counted_by_outcome(registry: ModelSchemaRegistry) -> None:
    binding = _binding()
    labels = {
        "project": "proj",
        "stream": binding.name,
        "connector": "http",
        "model": "JPCP",
        "outcome": "ok",
    }
    duration = {"project": "proj", "stream": binding.name, "connector": "http"}
    before = _sample("dataplane_stream_requests_total", labels)
    before_count = _sample("dataplane_stream_request_duration_seconds_count", duration)
    _ingress(registry).handle(binding, _req(binding))
    assert _sample("dataplane_stream_requests_total", labels) - before == 1
    after_count = _sample("dataplane_stream_request_duration_seconds_count", duration)
    assert after_count - before_count == 1


def test_two_projects_sharing_a_stream_name_never_merge_series(
    registry: ModelSchemaRegistry,
) -> None:
    """R9.5: the per-stream series carry the project."""
    name = f"s-{uuid.uuid4().hex[:8]}"
    ingress = _ingress(registry)
    for project, n in (("alpha", 2), ("beta", 1)):
        binding = _binding(name, project=project)
        for _ in range(n):
            ingress.handle(binding, _req(binding))

    def count(project: str) -> float:
        labels = {
            "project": project,
            "stream": name,
            "connector": "http",
            "model": "JPCP",
            "outcome": "ok",
        }
        return _sample("dataplane_stream_requests_total", labels)

    assert (count("alpha"), count("beta")) == (2.0, 1.0)


def test_a_refused_offer_counts_a_dropped_record(registry: ModelSchemaRegistry) -> None:
    binding = _binding()
    labels = {"project": "proj", "stream": binding.name}
    before = _sample("dataplane_stream_telemetry_dropped_total", labels)
    ingress = _ingress(registry, spool=_Spool(accept=False))
    ingress.handle(binding, _req(binding))
    assert _sample("dataplane_stream_telemetry_dropped_total", labels) - before == 1
    assert ingress.stats()[f"proj/{binding.name}"]["telemetry_dropped"] == 1


def test_spool_hooks_feed_the_spool_drop_and_unlabelled_failure_counters() -> None:
    on_drop, on_fail = metrics.spool_hooks()
    spool = {"project": "_spool", "stream": "_spool"}
    dropped = _sample("dataplane_stream_telemetry_dropped_total", spool)
    failed = _sample("dataplane_stream_telemetry_failed_total")
    on_drop()
    on_fail()
    on_fail()
    assert _sample("dataplane_stream_telemetry_dropped_total", spool) - dropped == 1
    assert _sample("dataplane_stream_telemetry_failed_total") - failed == 2


def test_ingress_sets_the_embedding_gauges(registry: ModelSchemaRegistry) -> None:
    model = f"M{uuid.uuid4().hex[:6]}"
    binding = _binding(model=model)
    _ingress(registry).handle(binding, _req(binding))
    assert _sample("dataplane_stream_embedding_norm", {"model": model}) == pytest.approx(5.0)
    assert _sample("dataplane_stream_embedding_mean", {"model": model}) == pytest.approx(3.5)
    assert _sample("dataplane_stream_embedding_std", {"model": model}) == pytest.approx(0.5)


def test_on_baseline_sets_the_baseline_gauges_and_skips_missing_keys() -> None:
    model = f"M{uuid.uuid4().hex[:6]}"
    metrics.on_baseline(model, {"norm_mean": 9.0, "mean_mean": "0.5", "std_mean": None})
    assert _sample("dataplane_stream_embedding_norm_baseline", {"model": model}) == 9.0
    assert _sample("dataplane_stream_embedding_mean_baseline", {"model": model}) == 0.5
    value = REGISTRY.get_sample_value("dataplane_stream_embedding_std_baseline", {"model": model})
    assert value is None  # unset, not a misleading zero


def test_connector_state_is_one_hot() -> None:
    stream = f"s-{uuid.uuid4().hex[:8]}"
    metrics.set_connector_state("proj", stream, "running")
    metrics.set_connector_state("proj", stream, "backoff")
    metrics.set_connector_state("other", stream, "running")  # another project, same stream name

    def state(project: str, value: str) -> float:
        labels = {"project": project, "stream": stream, "state": value}
        return _sample("dataplane_stream_connector_state", labels)

    assert (state("proj", "running"), state("proj", "backoff")) == (0, 1)
    assert state("other", "running") == 1


def test_concurrent_state_writers_leave_exactly_one_state_set() -> None:
    """M10: bookkeeping and gauge writes are one critical section."""
    stream = f"s-{uuid.uuid4().hex[:8]}"
    states = ("running", "backoff", "paused", "failed")
    barrier = threading.Barrier(len(states))

    def writer(value: str) -> None:
        barrier.wait(timeout=10)
        for _ in range(200):
            metrics.set_connector_state("proj", stream, value)

    threads = [threading.Thread(target=writer, args=(v,)) for v in states]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    total = sum(
        _sample(
            "dataplane_stream_connector_state", {"project": "proj", "stream": stream, "state": v}
        )
        for v in states
    )
    assert total == 1


def test_stats_are_per_stream(registry: ModelSchemaRegistry) -> None:
    ingress = _ingress(registry)
    a, b = _binding(), _binding()
    ingress.handle(a, _req(a))
    ingress.handle(a, _req(a))
    ingress.handle(b, replace(_req(b), payload={"x": float("inf")}))
    stats: dict[str, Any] = ingress.stats()
    assert stats[f"proj/{a.name}"]["requests"] == 2
    assert stats[f"proj/{a.name}"]["outcomes"] == {"ok": 2}
    assert stats[f"proj/{b.name}"]["outcomes"] == {"validation": 1}
    assert stats[f"proj/{b.name}"]["connector"] == "http"
