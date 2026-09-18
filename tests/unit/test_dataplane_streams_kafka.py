"""Kafka stream connector (ADR 0131, Plan 2, task A7 / E5): at-least-once over a consumer group.

``confluent_kafka`` is not installed here, so every test runs against fakes: a consumer that keeps
per-partition logs, honours pause/resume/seek, records store/commit calls and runs the rebalance
callbacks from inside ``poll()``; and a producer whose delivery reports fire from ``poll()``/
``flush()`` (or synchronously inside ``produce()``), and can be held or failed. The ingress is the
real :class:`StreamIngress` over a scripted inference client.

Determinism: sessions are driven one ``step()`` at a time on the test thread, with an injected
clock (backoff deadlines are clock deadlines — the loop never sleeps), a fixed jitter, and either
an inline executor or a manual one whose jobs the test runs in a chosen order. Only the two
``run()`` tests use real threads, bounded by short Event waits.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from examlops.dataplane.streams import kafka_stream as ks
from examlops.dataplane.streams.client import CALLER_STATUS
from examlops.dataplane.streams.dlq import LoggingDeadLetterSink, redact_error
from examlops.dataplane.streams.ingress import IngressResult, StreamIngress
from examlops.dataplane.streams.schema import ModelSchemaRegistry
from examlops.dataplane.streams.types import (
    InferenceResult,
    StreamBinding,
    StreamLimits,
    StreamRequest,
)
from examlops.dataplane.types import DataplaneError, EgressDenied, SpecError

TOPIC = "jobs"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

# ── fakes ────────────────────────────────────────────────────────────────────


class FakeError:
    def __init__(self, text: str = "broker down", *, code: int = -195, fatal: bool = False):
        self._text, self._code, self._fatal = text, code, fatal

    def code(self) -> int:
        return self._code

    def str(self) -> str:
        return self._text

    def fatal(self) -> bool:
        return self._fatal


class FakeTP:
    def __init__(self, topic: str, partition: int, offset: int = -1001):
        self.topic, self.partition, self.offset = topic, partition, offset


class FakeMsg:
    def __init__(
        self,
        partition: int,
        offset: int,
        value: bytes | None,
        *,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        error: FakeError | None = None,
    ):
        self._p, self._o, self._v = partition, offset, value
        self._k, self._h, self._e = key, headers, error

    def topic(self) -> str:
        return TOPIC

    def partition(self) -> int:
        return self._p

    def offset(self) -> int:
        return self._o

    def value(self) -> bytes | None:
        return self._v

    def key(self) -> bytes | None:
        return self._k

    def headers(self) -> list[tuple[str, bytes]] | None:
        return self._h

    def error(self) -> FakeError | None:
        return self._e


class FakeConsumer:
    """Eager-rebalance consumer: callbacks run inside poll(), then the assignment changes."""

    def __init__(self, conf: dict[str, Any]):
        self.conf = conf
        self.log: list[tuple[Any, ...]] = []
        self.data: dict[int, list[FakeMsg]] = {}
        self.committed_offsets: dict[int, int] = {}
        self.pos: dict[int, int] = {}
        self.assigned: list[int] = []
        self.paused: set[int] = set()
        self.callbacks: dict[str, Any] = {}
        self.events: deque[tuple[str, Any]] = deque()
        self.polls = 0
        self.poll_hook: Callable[[FakeConsumer], None] | None = None
        self.wait = False
        self._idle = threading.Event()
        self._rr = 0
        #: return messages from paused partitions with this probability (prefetched stragglers)
        self.straggle: Callable[[], bool] = lambda: False
        #: a poll that returns nothing although data is there (fetch latency after a seek)
        self.gap: Callable[[], bool] = lambda: False
        #: messages handed back by the next polls whatever the pause state (prefetched)
        self.injected: deque[FakeMsg] = deque()
        #: like auto.offset.reset=error: a seek below the log start stalls the partition and
        #: reports OFFSET_OUT_OF_RANGE until the next seek
        self.out_of_range_mode = False
        self.out_of_range: set[int] = set()
        self.log_starts: dict[int, int] = {}  # set when retention empties a partition
        self._explicit: dict[int, int] = {}
        self.store_hook: Callable[[list[tuple[int, int]]], None] | None = None
        self.seek_hook: Callable[[int, int], None] | None = None

    # test helpers
    def add(
        self,
        partition: int,
        value: bytes | None,
        *,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        offset: int | None = None,
    ) -> int:
        msgs = self.data.setdefault(partition, [])
        off = offset if offset is not None else (msgs[-1].offset() + 1 if msgs else 0)
        msgs.append(FakeMsg(partition, off, value, key=key, headers=headers))
        return off

    def message(self, partition: int, offset: int) -> FakeMsg:
        return next(m for m in self.data[partition] if m.offset() == offset)

    def _log_start(self, partition: int) -> int:
        msgs = self.data.get(partition, [])
        first = msgs[0].offset() if msgs else 0
        return max(first, self.log_starts.get(partition, 0))

    def get_watermark_offsets(self, tp: Any, timeout: float | None = None, cached=False):
        msgs = self.data.get(tp.partition, [])
        low = self._log_start(tp.partition)
        return low, max(low, msgs[-1].offset() + 1 if msgs else 0)

    def remove(self, partition: int, offset: int) -> None:
        """The broker drops a record (retention, compaction, DeleteRecords)."""
        self.data[partition] = [m for m in self.data[partition] if m.offset() != offset]

    def assign(self, partitions: list[Any]) -> None:
        """Test helper with partition numbers (queues a rebalance); the consumer API with
        TopicPartitions (inside ``on_assign``: explicit start offsets for this assignment)."""
        if partitions and not isinstance(partitions[0], int):
            self.log.append(("assign_offsets", [(tp.partition, tp.offset) for tp in partitions]))
            self._explicit = {tp.partition: tp.offset for tp in partitions if tp.offset >= 0}
            return
        self.events.append(("assign", partitions))

    def committed(self, tps: list[Any], timeout: float | None = None) -> list[Any]:
        return [
            FakeTP(tp.topic, tp.partition, self.committed_offsets.get(tp.partition, -1001))
            for tp in tps
        ]

    def revoke(self, partitions: list[int]) -> None:
        self.events.append(("revoke", partitions))

    def lose(self, partitions: list[int]) -> None:
        self.events.append(("lost", partitions))

    def stores(self, partition: int) -> list[int]:
        return [off for e in self.log if e[0] == "store" for p, off in e[1] if p == partition]

    def commits(self) -> list[tuple[list[tuple[int, int]], bool]]:
        return [(e[1], e[2]) for e in self.log if e[0] == "commit"]

    def names(self) -> list[str]:
        return [e[0] for e in self.log]

    # the consumer API
    def subscribe(self, topics, on_assign=None, on_revoke=None, on_lost=None):
        self.log.append(("subscribe", list(topics)))
        self.callbacks = {"assign": on_assign, "revoke": on_revoke, "lost": on_lost}

    def poll(self, timeout: float | None = None) -> FakeMsg | None:
        self.polls += 1
        if self.poll_hook is not None:
            self.poll_hook(self)
        while self.events:
            kind, arg = self.events.popleft()
            if kind == "error":
                self.conf["error_cb"](arg)
                continue
            if kind == "msg_error":
                return FakeMsg(0, -1, None, error=arg)
            if kind == "part_error":
                return FakeMsg(arg[0], -1, None, error=arg[1])
            tps = [FakeTP(TOPIC, p) for p in arg]
            self._explicit = {}
            self.callbacks[kind](self, tps)
            self.log.append(
                ({"assign": "assigned", "revoke": "revoked"}.get(kind, kind), sorted(arg))
            )
            if kind == "assign":
                self.assigned = sorted(set(self.assigned) | set(arg))
                for p in arg:
                    self.pos[p] = self._explicit.get(p, self.committed_offsets.get(p, 0))
            else:
                self.assigned = [p for p in self.assigned if p not in arg]
            self.paused -= set(arg)
        if self.injected:
            msg = self.injected.popleft()
            self.pos[msg.partition()] = msg.offset() + 1
            return msg
        if self.gap():
            return None
        order = self.assigned[self._rr :] + self.assigned[: self._rr]
        for p in order:
            if p in self.out_of_range or (p in self.paused and not self.straggle()):
                continue
            msg = next((m for m in self.data.get(p, []) if m.offset() >= self.pos[p]), None)
            if msg is not None:
                self.pos[p] = msg.offset() + 1
                self._rr = (self.assigned.index(p) + 1) % max(1, len(self.assigned))
                return msg
        if self.wait and timeout:
            self._idle.wait(min(timeout, 0.005))
        return None

    def pause(self, tps: list[Any]) -> None:
        parts = sorted(tp.partition for tp in tps)
        self.paused |= set(parts)
        self.log.append(("pause", parts))

    def resume(self, tps: list[Any]) -> None:
        parts = sorted(tp.partition for tp in tps)
        self.paused -= set(parts)
        self.log.append(("resume", parts))

    def seek(self, tp: Any) -> None:
        if self.seek_hook is not None and tp.offset >= 0:
            self.seek_hook(tp.partition, tp.offset)
        p, offset = tp.partition, tp.offset
        log_start = self._log_start(p)
        self.out_of_range.discard(p)
        if offset == -2:  # OFFSET_BEGINNING
            offset = log_start
        elif self.out_of_range_mode and offset < log_start:
            self.out_of_range.add(p)
            self.events.append(("part_error", (p, FakeError("offset out of range", code=1))))
        self.pos[p] = offset
        self.log.append(("seek", p, tp.offset))

    def store_offsets(self, message: Any = None, offsets: list[Any] | None = None) -> None:
        pairs = [(tp.partition, tp.offset) for tp in offsets or []]
        if self.store_hook is not None:
            self.store_hook(pairs)
        self.log.append(("store", pairs))

    def commit(self, message: Any = None, offsets: list[Any] | None = None, asynchronous=True):
        pairs = [(tp.partition, tp.offset) for tp in offsets or []]
        for p, off in pairs:
            self.committed_offsets[p] = off
        self.log.append(("commit", pairs, asynchronous))

    def close(self) -> None:
        self.log.append(("close",))


class FakeProducer:
    def __init__(self, conf: dict[str, Any]):
        self.conf = conf
        self.produced: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self._reports: list[tuple[Any, Any, dict[str, Any]]] = []
        self.hold = False  # poll() fires nothing while held
        self.stuck = False  # flush() fires nothing either
        self.sync = False  # the report fires inside produce()
        self.fail: Callable[[dict[str, Any]], FakeError | None] = lambda rec: None
        self.raise_on_produce: BaseException | None = None

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        if self.raise_on_produce is not None:
            raise self.raise_on_produce
        rec = {"topic": topic, "value": value, "key": key, "headers": dict(headers or [])}
        self.produced.append(rec)
        err = self.fail(rec)
        if self.sync:
            self._report(on_delivery, err, rec)
        else:
            self._reports.append((on_delivery, err, rec))

    def _report(self, cb: Any, err: Any, rec: dict[str, Any]) -> None:
        if err is None:
            self.delivered.append(rec)
        cb(err, None)

    def _fire(self) -> int:
        reports, self._reports = self._reports, []
        for cb, err, rec in reports:
            self._report(cb, err, rec)
        return len(reports)

    def poll(self, timeout: float = 0) -> int:
        return 0 if self.hold else self._fire()

    def flush(self, timeout: float | None = None) -> int:
        if not self.stuck:
            self._fire()
        return len(self._reports)

    def to(self, topic: str) -> list[dict[str, Any]]:
        return [r for r in self.produced if r["topic"] == topic]

    def delivered_to(self, topic: str) -> list[dict[str, Any]]:
        return [r for r in self.delivered if r["topic"] == topic]


def ok(prediction: float = 1.5) -> InferenceResult:
    return InferenceResult(
        "ok", prediction=prediction, body={"prediction": prediction, "model_version": "7"}
    )


def fail(outcome: str, *, retry_after: float | None = None, detail: str | None = None):
    """A failure as the inference client reports it: an ``overloaded`` answer carries the
    upstream status (``client.classify_response``), so it spends an attempt."""
    body: dict[str, Any] = {"error": outcome}
    if outcome == "overloaded":
        body["upstream"] = 503
    if detail:
        body["detail"] = detail
    return InferenceResult(
        outcome,  # type: ignore[arg-type]
        body=body,
        retry_after=retry_after,
        status=CALLER_STATUS[outcome],  # type: ignore[index]
    )


def bare_overloaded(retry_after: float | None = 0.5) -> InferenceResult:
    """An ``overloaded`` carrying neither ``upstream`` nor a ``shed_reason``: unknown, so it
    must count as a failed attempt (fail safe)."""
    return InferenceResult(
        "overloaded", body={"error": "overloaded"}, retry_after=retry_after, status=503
    )


class SheddingIngress:
    """Wraps the real ingress and sheds a request for ``x`` the first ``sheds[x]`` times, exactly
    as the ingress's own rate check would: ``overloaded``, ``shed_reason="rate"``, no inference."""

    def __init__(self, inner: StreamIngress, sheds: dict[Any, int], retry_after: float) -> None:
        self.inner, self.remaining, self.retry_after = inner, dict(sheds), retry_after
        self.shed: dict[Any, int] = {}
        self._lock = threading.Lock()

    def handle(self, binding: StreamBinding, req: StreamRequest, *, reply: Any = None) -> Any:
        x = req.payload.get("x")
        with self._lock:
            left = self.remaining.get(x, 0)
            if left > 0:
                self.remaining[x] = left - 1
                self.shed[x] = self.shed.get(x, 0) + 1
        if left > 0:
            result = IngressResult(
                outcome="overloaded",
                body={"error": "overloaded"},
                retry_after=self.retry_after,
                status=503,
                shed_reason="rate",
            )
            if reply is not None:
                reply(result)
            return result
        return self.inner.handle(binding, req, reply=reply)


class ScriptedClient:
    """Answers by the payload's ``x``: the n-th call for an ``x`` gets ``script[x][n-1]`` (the
    last entry repeats); anything unscripted is ``ok``."""

    def __init__(self, script: dict[Any, list[InferenceResult]] | None = None):
        self.script = script or {}
        self.calls: list[StreamRequest] = []
        self.answered: set[Any] = set()  # x values that got an ok/model answer
        self._lock = threading.Lock()

    def infer(self, req: StreamRequest, body: dict[str, Any]) -> InferenceResult:
        x = req.payload.get("x")
        with self._lock:
            self.calls.append(req)
            n = sum(1 for c in self.calls if c.payload.get("x") == x)
        seq = self.script.get(x)
        result = seq[min(n, len(seq)) - 1] if seq else ok(float(x) if isinstance(x, int) else 1.0)
        if result.outcome in ("ok", "model"):
            with self._lock:
                self.answered.add(x)
        return result

    def count(self, x: Any) -> int:
        return sum(1 for c in self.calls if c.payload.get("x") == x)


class _Spool:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def offer(self, record: Any) -> bool:
        self.records.append(record)
        return True


class _Coord:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def first_seen(self, key: str, ttl_s: float) -> bool:
        new = key not in self.seen
        self.seen.add(key)
        return new

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        return True


class RecordingSink:
    """Records dead letters; ``failures`` makes the next N ``record()`` calls raise."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.failures = 0
        self.fail_if: Callable[[], bool] = lambda: False
        self.on_record: Callable[[str, Any, dict[str, Any]], None] | None = None
        self.calls = 0

    def origins(self, *, expired: bool = True) -> set[tuple[int, int]]:
        return {
            (r["origin"]["partition"], r["origin"]["offset"])
            for r in self.records
            if expired or r["reason"] != "expired_from_log"
        }

    def record(self, binding, *, reason, error, attempts, payload, origin) -> None:
        self.calls += 1
        if self.failures > 0 or self.fail_if():
            self.failures = max(0, self.failures - 1)
            raise RuntimeError("dead-letter store down: password=hunter2")
        if self.on_record is not None:
            self.on_record(reason, payload, origin)
        self.records.append(
            {
                "stream": binding.name,
                "reason": reason,
                "error": error,
                "attempts": attempts,
                "payload": payload,
                "origin": origin,
            }
        )


class InlineExecutor:
    def __init__(self) -> None:
        self.workers: int | None = None

    def submit(self, fn: Callable[[], None]) -> None:
        fn()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class ManualExecutor:
    """Jobs wait until the test runs them, by dispatch index, in any order."""

    def __init__(self) -> None:
        self.jobs: list[Callable[[], None] | None] = []

    def submit(self, fn: Callable[[], None]) -> None:
        self.jobs.append(fn)

    def run(self, index: int) -> None:
        job = self.jobs[index]
        assert job is not None, f"job {index} already ran"
        self.jobs[index] = None
        job()

    def run_all(self) -> int:
        ran = 0
        for i, job in enumerate(self.jobs):
            if job is not None:
                self.run(i)
                ran += 1
        return ran

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _v(obj: Any) -> bytes:
    return json.dumps(obj).encode()


def _binding(**overrides: Any) -> StreamBinding:
    fields: dict[str, Any] = {
        "project": "proj",
        "name": "s1",
        "connector": "kafka",
        "model": "JPCP",
        "alias": "Production",
        "address": TOPIC,
        "connection": "kc",
        "options": {},
        "limits": StreamLimits(max_in_flight=4, max_attempts=3),
    }
    fields.update(overrides)
    return StreamBinding(**fields)


@dataclass
class Rig:
    connector: ks.KafkaStreamConnector
    session: ks.KafkaStreamSession
    consumer: FakeConsumer
    producer: FakeProducer | None
    client: ScriptedClient
    sink: RecordingSink
    executor: Any
    clock: Clock
    statuses: list[tuple[str, str | None]] = field(default_factory=list)
    ingress: Any = None
    served: Any = None

    def steps(self, n: int = 12) -> None:
        for _ in range(n):
            self.session.step()

    def settle(self, rounds: int = 40, advance: float = 0.0) -> None:
        """Step, running any manual jobs and advancing the clock, until nothing is left."""
        for _ in range(rounds):
            if isinstance(self.executor, ManualExecutor):
                self.executor.run_all()
            self.session.step()
            if advance:
                self.clock.advance(advance)


CONN = {"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092"}


@pytest.fixture(autouse=True)
def _loopback_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR 0130 §10: the bootstrap host is egress-checked; the fakes "connect" to loopback.
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")


def _rig(
    tmp_path: Path,
    *,
    binding: StreamBinding | None = None,
    executor: Any = None,
    script: dict[Any, list[InferenceResult]] | None = None,
    rng: Callable[[], float] = lambda: 0.5,
    conn: dict[str, Any] | None = None,
    start: bool = True,
    coord: Any = None,
    sheds: dict[Any, int] | None = None,
    shed_retry_after: float = 0.4,
) -> Rig:
    consumers: list[FakeConsumer] = []
    producers: list[FakeProducer] = []
    sink, clock, client = RecordingSink(), Clock(), ScriptedClient(script)
    executor = executor or InlineExecutor()
    statuses: list[tuple[str, str | None]] = []

    def consumer_factory(conf: dict[str, Any]) -> FakeConsumer:
        consumers.append(FakeConsumer(conf))
        return consumers[-1]

    def producer_factory(conf: dict[str, Any]) -> FakeProducer:
        producers.append(FakeProducer(conf))
        return producers[-1]

    def executor_factory(workers: int) -> Any:
        executor.workers = workers
        return executor

    connector = ks.KafkaStreamConnector(
        dlq=sink,
        consumer_factory=consumer_factory,
        producer_factory=producer_factory,
        resolve_connection=lambda name, project=None: dict(conn or CONN),
        executor_factory=executor_factory,
        clock=clock,
        rng=rng,
        poll_timeout_s=0.0,
    )
    ingress = StreamIngress(
        client, _Spool(), None, ModelSchemaRegistry(tmp_path), coord or _Coord()
    )
    served: Any = ingress
    if sheds is not None:
        served = SheddingIngress(ingress, sheds, shed_retry_after)
    session = connector.open_session(
        binding or _binding(), served, lambda s, d: statuses.append((s, d))
    )
    if start:
        session.start()
    return Rig(
        connector,
        session,
        consumers[0],
        producers[0] if producers else None,
        client,
        sink,
        executor,
        clock,
        statuses,
        ingress,
        served,
    )


def _with(**options: Any) -> StreamBinding:
    return _binding(options=options)


# ── the offset-store rule ────────────────────────────────────────────────────


def test_offset_is_stored_only_after_handle_returns(tmp_path: Path) -> None:
    rig = _rig(tmp_path, executor=ManualExecutor())
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.client.calls == [] and rig.consumer.stores(0) == []  # dispatched, not handled
    rig.executor.run(0)
    rig.steps()
    assert rig.client.count(1) == 1
    assert rig.consumer.stores(0) == [1]


def test_out_of_order_completion_stores_the_contiguous_watermark(tmp_path: Path) -> None:
    rig = _rig(tmp_path, executor=ManualExecutor())
    rig.consumer.committed_offsets[0] = 5
    for x, off in ((5, 5), (6, 6), (7, 7)):
        rig.consumer.add(0, _v({"x": x}), offset=off)
    rig.consumer.assign([0])
    rig.steps()
    assert len(rig.executor.jobs) == 3  # 5, 6, 7 all in flight on the pool
    rig.executor.run(0)  # offset 5
    rig.steps()
    assert rig.consumer.stores(0) == [6]
    rig.executor.run(2)  # offset 7 — 6 is still in flight
    rig.steps()
    assert rig.consumer.stores(0) == [6]
    rig.executor.run(1)  # offset 6
    rig.steps()
    assert rig.consumer.stores(0) == [6, 8]


def test_offset_gaps_do_not_stall_the_watermark(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    for off in (0, 1, 4):  # 2 and 3 compacted away
        rig.consumer.add(0, _v({"x": off}), offset=off)
    rig.consumer.assign([0])
    rig.steps()
    assert rig.consumer.stores(0)[-1] == 5


def test_reply_is_produced_and_the_offset_waits_for_its_delivery(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    rig.producer.hold = True
    rig.consumer.add(0, _v({"x": 2}), key=b"job-7", headers=[("traceparent", TRACEPARENT.encode())])
    rig.consumer.assign([0])
    rig.steps()
    (reply,) = rig.producer.to("replies")
    assert reply["key"] == b"job-7"
    assert reply["headers"] == {"traceparent": TRACEPARENT.encode()}
    assert json.loads(reply["value"]) == {
        "ok": True,
        "outcome": "ok",
        "prediction": 2.0,
        "model": "JPCP",
        "version": "7",
        "error": None,
    }
    assert rig.consumer.stores(0) == []  # (a) not yet: the reply is undelivered
    rig.producer.hold = False
    rig.steps()
    assert rig.consumer.stores(0) == [1]


def test_a_delivery_report_before_handle_returns_is_still_counted_once(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    rig.producer.sync = True  # the report fires inside produce(), before handle() returns
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(0, _v({"x": 2}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.consumer.stores(0) == [1, 2]
    assert len(rig.producer.to("replies")) == 2


def test_a_failed_reply_delivery_leaves_the_offset_unstored_and_retries(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    failures = [FakeError("delivery timed out")]
    rig.producer.fail = lambda rec: failures.pop() if failures else None
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.consumer.stores(0) == []
    assert ("pause", [0]) in rig.consumer.log
    assert rig.client.count(1) == 1
    rig.clock.advance(ks.backoff_delay(1, lambda: 0.5))  # 0.25 s
    rig.steps()
    assert rig.client.count(1) == 2  # retried — never acknowledged unanswered
    assert rig.consumer.stores(0) == [1]
    assert len(rig.producer.to("replies")) == 2


def test_a_reply_that_cannot_be_produced_is_retried(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    rig.producer.raise_on_produce = BufferError("queue full")
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.consumer.stores(0) == [] and ("pause", [0]) in rig.consumer.log
    rig.producer.raise_on_produce = None
    rig.clock.advance(1.0)
    rig.steps()
    assert rig.consumer.stores(0) == [1]


# ── retry: pause, wait, seek, resume ─────────────────────────────────────────


def test_a_503_pauses_the_partition_then_seeks_back_and_resumes(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={1: [fail("overloaded", retry_after=2.0), ok()]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(0, _v({"x": 2}))
    rig.consumer.assign([0])
    rig.steps()
    assert ("pause", [0]) in rig.consumer.log
    assert not any(e[0] == "seek" for e in rig.consumer.log)
    assert rig.consumer.stores(0) == [] and rig.client.count(2) == 0
    rig.clock.advance(1.9)
    rig.steps()
    assert not any(e[0] == "seek" for e in rig.consumer.log)  # Retry-After not yet elapsed
    rig.clock.advance(0.1)
    rig.steps()
    names = rig.consumer.names()
    seek_at = rig.consumer.log.index(("seek", 0, 0))
    assert names.index("resume") > seek_at  # seek back first, then resume
    assert rig.client.count(1) == 2 and rig.client.count(2) == 1
    assert rig.consumer.stores(0)[-1] == 2


def test_retry_after_is_clamped(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={1: [fail("overloaded", retry_after=10_000.0), ok()]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    rig.clock.advance(299.0)
    rig.steps()
    assert rig.client.count(1) == 1
    rig.clock.advance(1.0)
    rig.steps()
    assert rig.client.count(1) == 2 and rig.consumer.stores(0) == [1]


def test_backoff_is_full_jitter_exponential_and_capped() -> None:
    assert [ks.backoff_delay(n, lambda: 1.0) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]
    assert ks.backoff_delay(7, lambda: 1.0) == ks.BACKOFF_CAP_S
    assert ks.backoff_delay(10_000, lambda: 1.0) == ks.BACKOFF_CAP_S  # no overflow
    assert ks.backoff_delay(3, lambda: 0.25) == 0.5
    assert ks.backoff_delay(3, lambda: 0.0) == 0.0


def test_transport_and_deadline_back_off_without_retry_after(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        script={1: [fail("transport"), fail("deadline"), ok()]},
        binding=_binding(limits=StreamLimits(max_in_flight=4, max_attempts=5)),
        rng=lambda: 1.0,
    )
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.session.retrying_partitions() == [0]
    rig.clock.advance(0.49)
    rig.steps()
    assert rig.client.count(1) == 1
    rig.clock.advance(0.01)  # attempt 1 → 0.5 s
    rig.steps()
    assert rig.client.count(1) == 2
    rig.clock.advance(0.99)
    rig.steps()
    assert rig.client.count(1) == 2
    rig.clock.advance(0.01)  # attempt 2 → 1.0 s
    rig.steps()
    assert rig.client.count(1) == 3 and rig.consumer.stores(0) == [1]
    assert rig.sink.records == []


def test_one_paused_partition_does_not_stop_the_others(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={1: [fail("overloaded", retry_after=10.0), ok()]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(0, _v({"x": 2}))
    for x in (10, 11, 12):
        rig.consumer.add(1, _v({"x": x}))
    rig.consumer.assign([0, 1])
    rig.steps(20)
    assert rig.consumer.stores(1)[-1] == 3  # partition 1 flowed to its end
    assert rig.consumer.stores(0) == []
    assert rig.consumer.paused == {0}
    assert rig.client.count(2) == 0  # nothing more from the paused partition
    assert rig.statuses[-1][0] == "retrying" and "partition 0" in (rig.statuses[-1][1] or "")
    rig.clock.advance(10.0)
    rig.steps()
    assert rig.consumer.stores(0)[-1] == 2 and rig.consumer.paused == set()
    assert rig.statuses[-1] == ("running", None)


def test_redelivered_offsets_that_already_resolved_are_skipped(tmp_path: Path) -> None:
    rig = _rig(tmp_path, executor=ManualExecutor(), script={0: [fail("transport"), ok()]})
    for x in (0, 1, 2):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps()
    rig.executor.run(1)
    rig.executor.run(2)
    rig.executor.run(0)  # offset 0 fails after 1 and 2 succeeded
    rig.steps()
    assert rig.consumer.stores(0) == []  # 0 is outstanding
    rig.clock.advance(1.0)
    rig.settle()
    assert ("seek", 0, 0) in rig.consumer.log
    assert (rig.client.count(0), rig.client.count(1), rig.client.count(2)) == (2, 1, 1)
    assert rig.consumer.stores(0) == [3]


# ── the invariant: never store past a non-terminal offset (review C1/C2 regressions) ──


def _terminal_oracle(rig: Rig) -> Callable[[int, FakeMsg], bool]:
    """Terminal = answered (the client returned ok/model and, with a reply topic, a reply was
    delivered for its key) or dead-lettered (a sink record for its origin and, when configured,
    a delivered dlq copy). A failed attempt never counts."""

    def delivered(topic: str | None, key: bytes | None, answered: bool | None = None) -> bool:
        if topic is None:
            return True
        assert rig.producer is not None
        for r in rig.producer.delivered_to(topic):
            if r["key"] != key:
                continue
            if answered is None:
                return True
            if (json.loads(r["value"])["outcome"] in ("ok", "model")) == answered:
                return True
        return False

    def terminal(p: int, m: FakeMsg) -> bool:
        options = rig.session.binding.options
        reply_topic, dlq_topic = options.get("reply_topic"), options.get("dlq_topic")
        dead = (
            # dead-lettered, every part delivered; a message still in the log never counts as
            # dead-lettered by an expired_from_log record (that would be a false expiry)
            (p, m.offset()) in rig.sink.origins(expired=False)
            and delivered(dlq_topic, m.key())
            and delivered(reply_topic, m.key(), False)
        )
        try:
            x = json.loads(m.value() or b"").get("x")
        except (ValueError, AttributeError):
            x = None
        answered = x in rig.client.answered and delivered(reply_topic, m.key(), True)
        return dead or answered  # either branch (a re-review note: not a short-circuit)

    return terminal


def _guard(rig: Rig) -> list[tuple[int, int, int]]:
    """Check, at every store_offsets(), that every lower offset of the partition is terminal,
    and at every seek, that it targets the lowest offset awaiting re-fetch."""
    terminal = _terminal_oracle(rig)
    violations: list[tuple[int, int, int]] = []

    def check(pairs: list[tuple[int, int]]) -> None:
        for p, stored in pairs:
            for m in rig.consumer.data.get(p, []):
                if m.offset() < stored and not terminal(p, m):
                    violations.append((p, stored, m.offset()))

    def check_seek(p: int, offset: int) -> None:
        state = rig.session.partition_state(p)
        pending = sorted(state.refetch) if state is not None else []
        if not pending or offset != pending[0]:  # only ever the lowest pending offset
            violations.append((p, -1, offset))

    rig.consumer.store_hook = check
    rig.consumer.seek_hook = check_seek
    return violations


def test_the_store_position_never_passes_an_offset_awaiting_refetch() -> None:
    st = ks._PartitionState(TOPIC, 0)
    st.high = 9
    st.outstanding, st.refetch = {5}, {5}  # 5 was purged/parked; 6-8 already terminal
    assert st.position() == 5 and st.seek_target() == 5
    assert st.is_terminal(7) and not st.is_terminal(5) and not st.is_terminal(9)
    st.terminal(5)
    assert st.position() == 9 and st.outstanding == set()


def _probe_rig(tmp_path: Path, script: dict[Any, list[InferenceResult]]) -> Rig:
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script=script,
        binding=_binding(limits=StreamLimits(max_in_flight=3, max_attempts=5)),
    )
    for x in (0, 1):
        rig.consumer.add(0, _v({"x": x}))
    for x in (100, 101):
        rig.consumer.add(1, _v({"x": x}))
    rig.consumer.assign([0, 1])
    rig.steps(3)
    rig.executor.run(0)  # partition 0 offset 0: a transport failure
    rig.steps(1)
    rig.clock.advance(5)
    rig.steps(6)  # seek back; offset 0 is re-queued behind its in-flight sibling
    return rig


def test_an_operator_pause_after_a_seek_back_never_skips_the_requeued_offset(
    tmp_path: Path,
) -> None:
    """Review C1 (probe_a7_loss): the pause purged the re-queued offset 0 and the next store
    jumped over it; its redelivery was then dropped as already stored."""
    rig = _probe_rig(tmp_path, {0: [fail("transport"), ok()]})
    violations = _guard(rig)
    rig.session.set_paused(True)
    rig.steps(1)
    rig.session.set_paused(False)
    rig.steps(1)
    rig.settle(rounds=30, advance=1.0)
    assert violations == []
    assert rig.client.count(0) == 2 and 0 in rig.client.answered
    assert rig.consumer.stores(0)[-1] == 2
    rig.consumer.revoke([0, 1])
    rig.steps(1)
    assert (0, 2) in rig.consumer.commits()[-1][0]


def test_a_sibling_failure_after_a_seek_back_never_skips_the_requeued_offset(
    tmp_path: Path,
) -> None:
    """Review C1 (probe_a7_loss2): no operator involved — offset 1 fails while 0 is queued."""
    rig = _probe_rig(
        tmp_path, {0: [fail("transport"), ok()], 1: [fail("overloaded", retry_after=1.0), ok()]}
    )
    violations = _guard(rig)
    rig.executor.run(2)  # partition 0 offset 1 fails
    rig.steps(1)
    state = rig.session.partition_state(0)
    assert state is not None and state.refetch == {0, 1}  # both parked, both outstanding
    rig.clock.advance(1.0)
    rig.steps(1)
    rig.settle(rounds=30, advance=1.0)
    assert violations == []
    assert {e[2] for e in rig.consumer.log if e[0] == "seek" and e[1] == 0} == {0}
    assert rig.client.count(0) == 2 and rig.client.count(1) == 2
    assert rig.consumer.stores(0)[-1] == 2


def test_a_second_failure_before_the_refetch_never_seeks_forward(tmp_path: Path) -> None:
    """Review C2 (probe_a7_stall_min): after the seek to 0, before the fetch returns, offset 1
    fails too; the old loop sought forward to 1 and offset 0 was never fetched again."""
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={0: [fail("transport"), ok()], 1: [fail("transport"), ok()]},
    )
    violations = _guard(rig)
    for x in (0, 1, 2):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(4)  # 0, 1, 2 in flight
    rig.executor.run(0)
    rig.steps(1)  # offset 0 fails → parked
    rig.clock.advance(1.0)
    gaps = [True]
    rig.consumer.gap = lambda: bool(gaps) and gaps.pop()  # one empty poll: fetch latency
    rig.steps(1)  # seeks to 0; nothing fetched yet
    rig.executor.run(1)  # offset 1 fails before 0 is back
    rig.executor.run(2)
    rig.steps(1)
    rig.clock.advance(1.0)
    rig.settle(rounds=40, advance=1.0)
    seeks = [e for e in rig.consumer.log if e[0] == "seek"]
    assert all(off == 0 for _, _, off in seeks)  # never forward past the pending offset 0
    assert violations == []
    assert (rig.client.count(0), rig.client.count(1), rig.client.count(2)) == (2, 2, 1)
    assert rig.consumer.stores(0)[-1] == 3
    rig.consumer.add(0, _v({"x": 3}))
    rig.consumer.add(0, _v({"x": 4}))
    rig.settle(rounds=10)
    state = rig.session.partition_state(0)
    assert state is not None and state.outstanding == set()
    assert rig.consumer.stores(0)[-1] == 5


def test_a_message_parked_while_the_backlog_is_full_is_never_skipped(tmp_path: Path) -> None:
    """Review C1, third trigger: after a seek-back, a message arrives while the backlog is full
    (a prefetched straggler); it is parked, and the store position waits for it."""
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={0: [fail("transport"), ok()]},
        binding=_binding(limits=StreamLimits(max_in_flight=1, max_attempts=5)),
    )
    violations = _guard(rig)
    for x in (0, 1, 2):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(3)  # 0 in flight, 1 queued (backlog full), partition flow-paused
    rig.executor.run(0)  # 0 fails; the queued 1 is parked with it
    rig.steps(1)
    rig.clock.advance(1.0)
    rig.steps(2)  # seek back to 0: 0 in flight again, 1 queued, backlog full again
    rig.consumer.injected.append(rig.consumer.message(0, 2))  # a prefetched straggler
    rig.steps(1)
    state = rig.session.partition_state(0)
    assert state is not None and 2 in state.refetch  # parked, still outstanding
    rig.settle(rounds=30, advance=1.0)
    assert violations == []
    assert [rig.client.count(x) for x in (0, 1, 2)] == [2, 1, 1]
    assert rig.consumer.stores(0)[-1] == 3


def _lag(partition: int) -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "dataplane_stream_consumer_lag",
        {"project": "proj", "stream": "s1", "partition": str(partition)},
    )


def test_consumer_lag_is_published_per_partition_and_cleared_on_revoke(tmp_path: Path) -> None:
    """M14: ADR 0131 d11 names the series and nothing emitted it, so batch S3's
    ``DataplaneStreamConsumerLagHigh`` alert would have had nothing to fire on. The watermark is
    read from the client's cache, so this costs no broker round trip."""
    rig = _rig(tmp_path, script={0: [ok(), ok(), ok()]})
    for x in range(3):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(1)
    assert _lag(0) is not None  # the poll loop publishes it, at most once a second

    rig.settle(rounds=10)
    rig.session._report_lag()
    assert _lag(0) == 0.0  # served everything the partition holds

    for x in (3, 4):  # two messages published but not fetched yet
        rig.consumer.add(0, _v({"x": x}))
    rig.session._report_lag()
    assert _lag(0) == 2.0

    rig.consumer.revoke([0])
    rig.steps(2)
    assert _lag(0) is None  # the series is gone, not frozen at its last value


def test_a_client_without_a_cached_watermark_publishes_no_lag(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={0: [ok()]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps(1)

    def _unavailable(tp: Any, timeout: float | None = None, cached: bool = False) -> Any:
        raise RuntimeError("no cached watermark")

    rig.consumer.get_watermark_offsets = _unavailable  # type: ignore[method-assign]
    rig.session._report_lag()  # no raise, and the loop keeps going
    rig.settle(rounds=5)


def _expired_total() -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "dataplane_stream_messages_expired_total", {"project": "proj", "stream": "s1"}
        )
        or 0.0
    )


def test_a_parked_offset_that_left_the_log_is_expired_not_re_sought_forever(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-review N1 (probe_a7r_gone): offset 1 is parked, then the broker drops it. The old loop
    re-sought offset 1 on every iteration and the partition never moved again."""
    caplog.set_level(logging.WARNING)
    before = _expired_total()
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={1: [fail("transport"), ok()]},
        binding=_with(reply_topic="replies"),
    )
    violations = _guard(rig)
    for x in range(4):
        rig.consumer.add(0, _v({"x": x}), key=f"k{x}".encode())
    rig.consumer.assign([0])
    rig.steps(5)
    rig.executor.run_all()
    rig.steps(2)  # offset 1 fails → parked; 0, 2, 3 answered
    state = rig.session.partition_state(0)
    assert state is not None and state.refetch == {1}
    rig.consumer.remove(0, 1)  # retention / compaction / DeleteRecords
    rig.clock.advance(2.0)
    rig.settle(rounds=30, advance=0.5)
    seeks = [e for e in rig.consumer.log if e[0] == "seek"]
    assert seeks == [("seek", 0, 1)]  # one seek, not one per iteration
    (dead,) = rig.sink.records
    assert dead["reason"] == "expired_from_log" and dead["payload"] is None
    assert dead["origin"] == {"topic": TOPIC, "partition": 0, "offset": 1}
    assert _expired_total() - before == 1
    assert rig.producer is not None
    expired = [
        r for r in rig.producer.to("replies") if json.loads(r["value"])["outcome"] == "expired"
    ]
    assert [r["key"] for r in expired] == [b"k1"]  # answered, keyed by the lost request
    assert json.loads(expired[0]["value"])["ok"] is False
    assert rig.consumer.stores(0)[-1] == 4 and state.outstanding == set()
    assert "left the log" in caplog.text and '{"x": 1}' not in caplog.text
    rig.consumer.add(0, _v({"x": 4}), key=b"k4")
    rig.settle(rounds=10)
    assert rig.client.count(4) == 1 and rig.consumer.stores(0)[-1] == 5
    assert len([e for e in rig.consumer.log if e[0] == "seek"]) == 1
    assert violations == []


def test_compaction_removing_a_parked_middle_offset_expires_only_that_offset(
    tmp_path: Path,
) -> None:
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={1: [fail("transport"), ok()], 3: [fail("transport"), ok()]},
        binding=_with(dlq_topic="jobs.dlq"),
    )
    violations = _guard(rig)
    for x in range(6):
        rig.consumer.add(0, _v({"x": x}), key=f"k{x}".encode())
    rig.consumer.assign([0])
    rig.steps(6)
    rig.executor.run_all()
    rig.steps(2)  # offsets 1 and 3 fail → parked, with whatever was queued behind them
    state = rig.session.partition_state(0)
    assert state is not None and {1, 3} <= state.refetch
    rig.consumer.remove(0, 3)  # compaction: a later record superseded key k3; 1 is still there
    rig.clock.advance(2.0)
    rig.settle(rounds=40, advance=0.5)
    assert rig.client.count(1) == 2 and 1 in rig.client.answered  # still there: retried
    assert rig.client.count(3) == 1  # gone: never inferred again
    assert [(r["reason"], r["origin"]["offset"]) for r in rig.sink.records] == [
        ("expired_from_log", 3)
    ]
    (copy,) = rig.producer.to("jobs.dlq") if rig.producer else [None]
    assert copy is not None and copy["value"] is None and copy["key"] == b"k3"
    assert rig.consumer.stores(0)[-1] == 6 and state.outstanding == set()
    assert len([e for e in rig.consumer.log if e[0] == "seek"]) <= 3
    assert violations == []


@pytest.mark.parametrize("down", ["jobs.dlq", "replies"])
def test_a_part_written_dead_letter_that_leaves_the_log_is_finished_not_re_expired(
    tmp_path: Path, down: str
) -> None:
    """Re-review 2 (probe_a7rr2_dl_then_gone): the sink record for a rejected message is written
    with its payload, its dlq copy (or its reply) keeps failing, then the offset leaves the log.
    Finish only the missing part — no second sink record (A7b upserts on origin: it would
    overwrite the payload-bearing row), no second or contradicting reply, no `expired`."""
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={1: [fail("validation", detail="bad field")]},
        binding=_with(reply_topic="replies", dlq_topic="jobs.dlq"),
    )
    assert rig.producer is not None
    state = {"down": True}
    rig.producer.fail = lambda rec: (
        FakeError("topic down")
        if state["down"] and rec["topic"] == down and rec["key"] == b"k1"
        else None
    )
    for x in range(4):
        rig.consumer.add(0, _v({"x": x, "field": "PAYLOAD"}), key=f"k{x}".encode())
    rig.consumer.assign([0])
    for _ in range(6):
        rig.executor.run_all()
        rig.session.step()
    parked = rig.session.partition_state(0)
    assert parked is not None and 1 in parked.dead_letter_next
    assert parked.dead_letter_next[1].sink_done  # the payload-bearing record is written
    rig.consumer.remove(0, 1)  # retention / compaction takes the parked offset
    state["down"] = False
    for _ in range(30):
        rig.executor.run_all()
        rig.session.step()
        rig.clock.advance(1.0)
    records = [r for r in rig.sink.records if r["origin"]["offset"] == 1]
    assert [(r["reason"], r["payload"] is not None) for r in records] == [("validation", True)]
    replies = [
        json.loads(r["value"])["outcome"]
        for r in rig.producer.delivered_to("replies")
        if r["key"] == b"k1"
    ]
    assert replies == ["validation"]  # exactly one answer, the original one
    copies = [r for r in rig.producer.delivered_to("jobs.dlq") if r["key"] == b"k1"]
    assert len(copies) == 1  # the copy is delivered once (value-less if it went after the loss)
    if down == "jobs.dlq":
        assert copies[0]["value"] is None and copies[0]["headers"]["x-examlops-error"]
    assert rig.consumer.stores(0)[-1] == 4 and parked.outstanding == set()


def test_an_exhausted_retry_that_leaves_the_log_keeps_its_reason(tmp_path: Path) -> None:
    """A dead letter owed but not yet written at all (retries exhausted, awaiting its re-fetch)
    keeps its own reason and reply outcome when the message leaves the log."""
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script={1: [fail("transport")]},
        binding=_binding(options={"reply_topic": "replies"}, limits=StreamLimits(max_attempts=1)),
    )
    for x in range(4):
        rig.consumer.add(0, _v({"x": x}), key=f"k{x}".encode())
    rig.consumer.assign([0])
    rig.steps(5)
    rig.executor.run_all()
    rig.consumer.gap = lambda: True  # the re-fetch has not come back yet…
    rig.steps(1)  # offset 1: exhausted after its one attempt → a dead letter is owed, sought
    state = rig.session.partition_state(0)
    assert state is not None and 1 in state.dead_letter_next and 1 in state.refetch
    rig.consumer.remove(0, 1)  # …and by the time it does, the message is gone
    rig.consumer.gap = lambda: False
    rig.settle(rounds=30, advance=1.0)
    (dead,) = rig.sink.records
    assert dead["reason"] == "retries_exhausted" and dead["payload"] is None
    assert rig.producer is not None
    assert [
        json.loads(r["value"])["outcome"]
        for r in rig.producer.delivered_to("replies")
        if r["key"] == b"k1"
    ] == ["retries_exhausted"]
    assert rig.consumer.stores(0)[-1] == 4


def test_an_explicit_out_of_range_error_expires_the_parked_offsets(tmp_path: Path) -> None:
    """librdkafka's OFFSET_OUT_OF_RANGE (auto.offset.reset=error semantics in the fake: the
    partition stalls until it is sought again) — handled the same way: log start, then expire."""
    rig = _rig(tmp_path, executor=ManualExecutor(), script={1: [fail("transport"), ok()]})
    violations = _guard(rig)
    rig.consumer.out_of_range_mode = True
    for x in range(4):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(5)
    rig.executor.run_all()
    rig.steps(2)  # offset 1 parked
    rig.consumer.remove(0, 0)
    rig.consumer.remove(0, 1)  # retention moved the log start to 2
    rig.clock.advance(2.0)
    rig.settle(rounds=30, advance=0.5)
    seeks = [(e[1], e[2]) for e in rig.consumer.log if e[0] == "seek"]
    assert seeks == [(0, 1), (0, -2)]  # the parked offset, then the log start
    assert [(r["reason"], r["origin"]["offset"]) for r in rig.sink.records] == [
        ("expired_from_log", 1)
    ]
    assert rig.consumer.stores(0)[-1] == 4 and violations == []


def test_retention_emptying_the_log_is_resolved_by_the_low_watermark(tmp_path: Path) -> None:
    """No record follows the parked offset (retention took the whole log): no first message will
    ever arrive to close the check, so the out-of-range error's low watermark decides."""
    rig = _rig(tmp_path, executor=ManualExecutor(), script={2: [fail("transport"), ok()]})
    violations = _guard(rig)
    rig.consumer.out_of_range_mode = True
    for x in range(3):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(4)
    rig.executor.run_all()
    rig.steps(2)  # offset 2, the last, is parked
    for off in (0, 1, 2):
        rig.consumer.remove(0, off)
    rig.consumer.log_starts[0] = 3  # retention emptied the partition: log start = end = 3
    rig.clock.advance(2.0)
    rig.settle(rounds=20, advance=0.5)
    assert [(r["reason"], r["origin"]["offset"]) for r in rig.sink.records] == [
        ("expired_from_log", 2)
    ]
    assert rig.consumer.stores(0)[-1] == 3 and violations == []


def test_an_expired_dead_letter_is_retried_from_memory_without_seeking(tmp_path: Path) -> None:
    rig = _rig(tmp_path, executor=ManualExecutor(), script={1: [fail("transport"), ok()]})
    for x in range(3):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    rig.steps(4)
    rig.executor.run_all()
    rig.steps(2)
    rig.consumer.remove(0, 1)
    rig.sink.failures = 2  # the dead-letter store is down when the expiry is found
    rig.clock.advance(2.0)
    rig.settle(rounds=40, advance=1.0)
    assert rig.sink.calls == 3 and [r["reason"] for r in rig.sink.records] == ["expired_from_log"]
    assert [e for e in rig.consumer.log if e[0] == "seek"] == [("seek", 0, 1)]  # no re-seeking
    assert rig.consumer.stores(0)[-1] == 3


def test_per_partition_memory_stays_bounded(tmp_path: Path) -> None:
    """Only non-terminal offsets are kept — bounded by pool + backlog + parked stragglers —
    over a long run with out-of-order completion and failures, and behind a head-of-line
    message that is shed 50 times before it is answered."""
    import random

    rnd = random.Random(7)
    script = {x: [fail("transport"), ok()] for x in range(1, 1500) if x % 10 == 0}
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script=script,
        sheds={0: 50},
        shed_retry_after=0.5,
        binding=_binding(limits=StreamLimits(max_in_flight=4, max_attempts=3)),
        rng=rnd.random,
    )
    violations = _guard(rig)
    for x in range(1500):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    peak = 0
    for _ in range(20_000):
        runnable = [i for i, job in enumerate(rig.executor.jobs) if job is not None]
        rnd.shuffle(runnable)
        for i in runnable[: rnd.randint(0, len(runnable))]:
            rig.executor.run(i)
        rig.session.step()
        rig.clock.advance(0.2)
        state = rig.session.partition_state(0)
        assert state is not None
        peak = max(peak, len(state.outstanding))
        if (rig.consumer.stores(0) or [0])[-1] == 1500:
            break
    assert violations == []
    assert rig.consumer.stores(0)[-1] == 1500 and len(rig.client.answered) == 1500
    assert rig.served.shed[0] == 50 and rig.client.count(0) == 1  # shed 50 times, then answered
    assert rig.sink.records == []
    assert peak <= rig.session.capacity * 2 + 2  # in flight + queued (+ the parked head)


# ── the at-least-once property over many interleavings (review I1) ─────────────────


def _chaos_run(tmp_path: Path, seed: int) -> list[str]:
    """One chaotic run; returns what went wrong (empty when the invariant held throughout)."""
    import random

    rnd = random.Random(seed)
    per_partition = 8
    script: dict[Any, list[InferenceResult]] = {}
    sheds: dict[Any, int] = {}
    poison: set[tuple[int, int]] = set()
    for p in (0, 1):
        for i in range(per_partition):
            x = p * 100 + i
            roll, k = rnd.random(), rnd.randint(1, 3)
            if roll < 0.45:
                continue
            if roll < 0.6:
                script[x] = [fail("transport")] * k + [ok()]  # k == 3 → exhausted
            elif roll < 0.68:
                script[x] = [fail("overloaded", retry_after=rnd.choice((None, 0.5)))] * k + [ok()]
            elif roll < 0.76:
                sheds[x] = rnd.randint(1, 6)  # the ingress's own rate shed: never counted
            elif roll < 0.82:
                script[x] = [fail("deadline")] * k + [ok()]
            elif roll < 0.88:
                script[x] = [fail("model", detail="bad row")]
            elif roll < 0.94:
                script[x] = [fail(rnd.choice(("validation", "not_found", "unexpected")))]
            else:
                poison.add((p, i))
    options: dict[str, Any] = {"reply_topic": "replies"}
    if rnd.random() < 0.5:
        options["dlq_topic"] = "jobs.dlq"
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        script=script,
        binding=_binding(
            options=options,
            limits=StreamLimits(max_in_flight=rnd.randint(1, 4), max_attempts=3),
        ),
        rng=rnd.random,
        sheds=sheds,
    )
    for p in (0, 1):
        for i in range(per_partition):
            value = b"{broken" if (p, i) in poison else _v({"x": p * 100 + i})
            rig.consumer.add(p, value, key=f"{p}:{i}".encode())
    violations = _guard(rig)
    terminal = _terminal_oracle(rig)
    assert rig.producer is not None
    chaos = {"on": True}
    rig.consumer.straggle = lambda: chaos["on"] and rnd.random() < 0.25
    rig.consumer.gap = lambda: chaos["on"] and rnd.random() < 0.2
    rig.producer.fail = lambda rec: (
        FakeError("delivery failed") if chaos["on"] and rnd.random() < 0.15 else None
    )
    rig.sink.fail_if = lambda: chaos["on"] and rnd.random() < 0.2
    rig.consumer.assign([0, 1])

    def finished() -> bool:
        return all(terminal(p, m) for p in (0, 1) for m in rig.consumer.data[p]) and all(
            (rig.consumer.stores(p) or [0])[-1] == per_partition for p in (0, 1)
        )  # a removed last offset is still stored past: its expired dead letter is terminal

    removed: set[tuple[int, int]] = set()
    false_expiry: list[tuple[Any, ...]] = []

    def on_record(reason: str, payload: Any, origin: dict[str, Any]) -> None:
        if reason != "expired_from_log":
            return
        p, offset = origin["partition"], origin["offset"]
        if any(m.offset() == offset for m in rig.consumer.data[p]):
            false_expiry.append((p, offset))  # expired while still in the log
        if payload is not None:
            false_expiry.append(("payload", p, offset))

    rig.sink.on_record = on_record
    for step in range(2500):
        if step == 400:  # the chaos stops; everything must then finish
            chaos["on"] = False
            rig.session.set_paused(False)
        if chaos["on"] and rnd.random() < 0.03:
            rig.session.set_paused(not rig.session.paused)
        if chaos["on"] and rnd.random() < 0.02:  # retention/compaction takes a parked offset
            p = rnd.choice((0, 1))
            state = rig.session.partition_state(p)
            last = rig.consumer.data[p][-1].offset()
            candidates = sorted(r for r in state.refetch if r < last) if state else []
            if candidates:  # the broker never drops a record with nothing after it
                gone = rnd.choice(candidates)
                rig.consumer.remove(p, gone)
                removed.add((p, gone))
        runnable = [i for i, job in enumerate(rig.executor.jobs) if job is not None]
        rnd.shuffle(runnable)
        for i in runnable[: rnd.randint(0, len(runnable))]:
            rig.executor.run(i)
        rig.producer.hold = chaos["on"] and rnd.random() < 0.3
        rig.session.step()
        rig.clock.advance(rnd.uniform(0.1, 1.0))
        if not chaos["on"] and finished():
            break
    problems = [f"seed={seed}: stored past a non-terminal offset {v}" for v in violations]
    problems += [f"seed={seed}: false expiry {x}" for x in false_expiry]
    dead_lettered = rig.sink.origins()
    if not removed <= dead_lettered:  # every parked offset that left the log was dead-lettered
        problems.append(f"seed={seed}: gone offsets not expired {sorted(removed - dead_lettered)}")
    for p, offset in removed:  # one record per origin, never a second (upsert-safe)
        if (
            sum(
                1
                for r in rig.sink.records
                if (r["origin"]["partition"], r["origin"]["offset"]) == (p, offset)
            )
            > 1
        ):
            problems.append(f"seed={seed}: two dead-letter records for gone offset {(p, offset)}")
    if not finished():  # every message eventually terminal, every offset stored
        problems.append(f"seed={seed}: did not finish")
    for p in (0, 1):
        stores = rig.consumer.stores(p)
        if stores != sorted(stores):
            problems.append(f"seed={seed}: store regressed on partition {p}")
        state = rig.session.partition_state(p)
        if state is None or state.outstanding or state.refetch:
            problems.append(f"seed={seed}: partition {p} left work outstanding")
    return problems


@pytest.mark.parametrize("block", range(10))
def test_at_least_once_holds_over_a_thousand_interleavings(tmp_path: Path, block: int) -> None:
    """At every store_offsets(), every lower offset is terminal; at the end every message is.
    Jobs finish in shuffled order; messages fail (counted, exhausted, shed), are rejected or
    poison; deliveries and the sink fail; paused partitions hand back stragglers; polls come
    back empty; the stream is paused and resumed. 10 × 100 seeds."""
    problems = [
        problem
        for seed in range(block * 100, block * 100 + 100)
        for problem in _chaos_run(tmp_path, seed)
    ]
    assert problems == []


# ── dead letters ─────────────────────────────────────────────────────────────


def test_dead_letter_after_max_attempts_with_its_headers(tmp_path: Path) -> None:
    raw = _v({"x": 1, "note": "sensitive"})
    rig = _rig(
        tmp_path,
        binding=_with(dlq_topic="jobs.dlq"),
        script={1: [fail("transport", detail="upstream refused, token=abc123secret")]},
        rng=lambda: 1.0,
    )
    rig.consumer.add(0, raw, key=b"k1")
    rig.consumer.assign([0])
    rig.settle(advance=5.0)
    assert rig.client.count(1) == 3  # max_attempts
    (dead,) = rig.sink.records
    assert dead["reason"] == "retries_exhausted" and dead["attempts"] == 3
    assert dead["payload"] == raw
    assert dead["origin"] == {"topic": TOPIC, "partition": 0, "offset": 0}
    assert "transport after 3 attempts" in dead["error"] and "abc123secret" not in dead["error"]
    assert rig.producer is not None
    (copy,) = rig.producer.to("jobs.dlq")
    assert copy["value"] == raw and copy["key"] == b"k1"
    headers = copy["headers"]
    assert set(headers) == set(ks.DLQ_HEADERS)
    assert headers["x-examlops-attempts"] == b"3"
    assert headers["x-examlops-stream"] == b"proj/s1"
    assert json.loads(headers["x-examlops-origin"]) == {
        "topic": TOPIC,
        "partition": 0,
        "offset": 0,
    }
    error = headers["x-examlops-error"].decode()
    assert error.startswith("transport after 3 attempts") and "abc123secret" not in error
    assert rig.consumer.stores(0) == [1]


def test_exhausted_retry_without_a_dlq_topic_records_and_stores(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={1: [fail("deadline")]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(0, _v({"x": 2}))
    rig.consumer.assign([0])
    rig.settle(advance=5.0)
    assert rig.client.count(1) == 3
    assert [r["reason"] for r in rig.sink.records] == ["retries_exhausted"]
    assert rig.producer is None
    assert rig.consumer.stores(0)[-1] == 2


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"{not json", "not_json"),
        (b"\xff\xfe\x00garbage", "not_json"),
        (b"[1, 2, 3]", "invalid_message"),
        (b'{"payload": {"x": 1}, "model": 7}', "invalid_message"),
        (b'{"payload": {"x": 1}, "metadata": "nope"}', "invalid_message"),
        (None, "not_json"),
    ],
)
def test_a_poison_message_is_dead_lettered_at_once_and_stored(
    tmp_path: Path, value: bytes | None, reason: str
) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(0, value)
    rig.consumer.add(0, _v({"x": 2}))
    rig.consumer.assign([0])
    rig.steps()
    (dead,) = rig.sink.records
    assert dead["reason"] == reason and dead["attempts"] == 1 and dead["payload"] == value
    assert rig.client.count(2) == 1 and len(rig.client.calls) == 1  # the poison never inferred
    assert rig.consumer.stores(0)[-1] == 2


def test_oversize_is_checked_on_the_raw_bytes_before_any_parse(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_binding(limits=StreamLimits(max_bytes=16)))
    rig.consumer.add(0, b"{" + b"x" * 40)  # oversize AND not JSON: oversize must win
    rig.consumer.assign([0])
    rig.steps()
    (dead,) = rig.sink.records
    assert dead["reason"] == "oversize" and "the limit is 16" in dead["error"]
    assert rig.client.calls == [] and rig.consumer.stores(0) == [1]


@pytest.mark.parametrize("outcome", ["validation", "not_found", "unexpected"])
def test_non_retryable_outcomes_are_dead_lettered_once_and_answered(
    tmp_path: Path, outcome: str
) -> None:
    """R15: a rejected request still gets its answer — ``ok:false``, the outcome, and a redacted
    error — and the offset waits for that reply as well as the dead-letter record."""
    rig = _rig(
        tmp_path,
        binding=_with(reply_topic="replies"),
        script={1: [fail(outcome, detail="field embedding: password=hunter2")]},
    )
    assert rig.producer is not None
    rig.producer.hold = True
    rig.consumer.add(0, _v({"x": 1}), key=b"k1", headers=[("traceparent", TRACEPARENT.encode())])
    rig.consumer.assign([0])
    rig.steps()
    (dead,) = rig.sink.records
    assert dead["reason"] == outcome and dead["attempts"] == 1
    assert rig.client.count(1) == 1
    (reply,) = rig.producer.to("replies")
    doc = json.loads(reply["value"])
    assert doc["ok"] is False and doc["outcome"] == outcome and doc["prediction"] is None
    assert "field embedding" in doc["error"] and "hunter2" not in doc["error"]
    assert reply["key"] == b"k1" and reply["headers"] == {"traceparent": TRACEPARENT.encode()}
    assert rig.consumer.stores(0) == []  # the reply is not delivered yet
    rig.producer.hold = False
    rig.steps()
    assert rig.consumer.stores(0) == [1]


def test_an_envelope_naming_another_model_is_refused_as_validation(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(0, _v({"payload": {"x": 1}, "model": "OTHER"}))
    rig.consumer.assign([0])
    rig.steps()
    (dead,) = rig.sink.records
    assert dead["reason"] == "validation" and "bound to model JPCP" in dead["error"]
    assert rig.client.calls == []


def test_a_model_failure_gets_an_error_reply_and_is_stored(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        binding=_with(reply_topic="replies"),
        script={1: [fail("model", detail="bad row password=hunter2")]},
    )
    rig.consumer.add(0, _v({"x": 1}), key=b"k")
    rig.consumer.assign([0])
    rig.steps()
    assert rig.producer is not None
    (reply,) = rig.producer.to("replies")
    doc = json.loads(reply["value"])
    assert doc["ok"] is False and doc["outcome"] == "model" and doc["prediction"] is None
    assert doc["model"] == "JPCP" and "bad row" in doc["error"] and "hunter2" not in doc["error"]
    assert rig.sink.records == [] and rig.consumer.stores(0) == [1]


@pytest.mark.parametrize(
    ("value", "limits", "script", "outcome", "reason"),
    [
        (b"{not json", StreamLimits(), {}, "validation", "not_json"),
        (b"[1]", StreamLimits(), {}, "validation", "invalid_message"),
        (
            b'{"x": 1, "pad": "' + b"y" * 64 + b'"}',
            StreamLimits(max_bytes=32),
            {},
            "validation",
            "oversize",
        ),
        (
            _v({"x": 1}),
            StreamLimits(max_attempts=2),
            {1: [fail("transport")]},
            "retries_exhausted",
            "retries_exhausted",
        ),
    ],
)
def test_every_terminal_outcome_gets_a_reply(
    tmp_path: Path,
    value: bytes,
    limits: StreamLimits,
    script: dict[Any, list[InferenceResult]],
    outcome: str,
    reason: str,
) -> None:
    rig = _rig(
        tmp_path, binding=_binding(options={"reply_topic": "replies"}, limits=limits), script=script
    )
    rig.consumer.add(0, value, key=b"job-3")
    rig.consumer.assign([0])
    rig.settle(advance=5.0)
    assert rig.producer is not None
    (reply,) = rig.producer.to("replies")
    doc = json.loads(reply["value"])
    assert doc["ok"] is False and doc["outcome"] == outcome and reply["key"] == b"job-3"
    assert doc["error"] and len(doc["error"].encode()) <= 512
    assert [r["reason"] for r in rig.sink.records] == [reason]
    assert rig.consumer.stores(0) == [1]


def test_a_failing_dead_letter_sink_is_retried_without_re_inferring(tmp_path: Path) -> None:
    """I3: record() raising is never swallowed — the offset is not stored, and after a backoff
    only the dead-letter write is redone."""
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"), script={1: [fail("not_found")]})
    rig.sink.failures = 2
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(0, _v({"x": 2}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.sink.calls == 1 and rig.sink.records == []
    assert rig.consumer.stores(0) == []  # nothing stored past the unwritten dead letter
    assert rig.producer is not None and rig.producer.to("replies") == []  # sink first
    assert rig.statuses[-1][0] == "retrying" and "dlq_write" in (rig.statuses[-1][1] or "")
    rig.settle(advance=5.0)
    assert rig.sink.calls == 3 and len(rig.sink.records) == 1
    assert rig.client.count(1) == 1  # inference ran once
    assert [json.loads(r["value"])["outcome"] for r in rig.producer.to("replies")] == [
        "not_found",
        "ok",
    ]
    assert rig.consumer.stores(0)[-1] == 2


def test_a_failing_dlq_topic_blocks_the_offset_until_the_copy_is_delivered(
    tmp_path: Path,
) -> None:
    """I3: a dlq_topic delivery failure is retried for as long as it takes; parts already
    written (the sink record, the reply) are not written again."""
    rig = _rig(tmp_path, binding=_with(dlq_topic="jobs.dlq", reply_topic="replies"))
    assert rig.producer is not None
    down = {"n": 4}

    def flaky(rec: dict[str, Any]) -> FakeError | None:
        if rec["topic"] == "jobs.dlq" and down["n"] > 0:
            down["n"] -= 1
            return FakeError("dlq topic down")
        return None

    rig.producer.fail = flaky
    rig.consumer.add(0, b"{not json")
    rig.consumer.assign([0])
    rig.steps()
    assert rig.consumer.stores(0) == []
    rig.settle(rounds=60, advance=5.0)
    assert len(rig.producer.to("jobs.dlq")) == 5  # 4 failures, then delivered
    assert len(rig.producer.to("replies")) == 1  # the reply was delivered the first time
    assert len(rig.sink.records) == 1  # the sink is written once
    assert rig.consumer.stores(0) == [1]


def test_dead_letter_error_text_is_redacted_and_bounded() -> None:
    text = "password=hunter2 " + "é" * 2000
    out = redact_error(text)
    assert "hunter2" not in out
    assert len(out.encode("utf-8")) <= 512
    out.encode("utf-8").decode("utf-8")  # never a split character
    assert redact_error("token=abc123", secrets=["abc123"]).count("abc123") == 0


def test_the_logging_sink_logs_metadata_never_the_payload(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    LoggingDeadLetterSink().record(
        _binding(),
        reason="validation",
        error="field embedding: token=s3cr3tvalue",
        attempts=2,
        payload=b'{"ssn": "123-45-6789"}',
        origin={"topic": TOPIC, "partition": 3, "offset": 9},
    )
    text = caplog.text
    assert "reason=validation" in text and "attempts=2" in text and '"offset": 9' in text
    assert "123-45-6789" not in text and "s3cr3tvalue" not in text
    assert "sha256=" in text and "size=22" in text


# ── backpressure the stream causes itself is not a failed attempt (review I4) ────────


class _WindowCoord(_Coord):
    """A rate limiter over the test clock: ``limit`` admissions per ``window_s``."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.admitted: dict[str, list[float]] = {}

    def allow(self, bucket: str, limit: int, window_s: float) -> bool:
        now = self.clock()
        recent = [t for t in self.admitted.get(bucket, []) if now - t < window_s]
        if len(recent) >= limit:
            self.admitted[bucket] = recent
            return False
        self.admitted[bucket] = [*recent, now]
        return True


def test_a_backlog_replay_under_a_low_rate_limit_dead_letters_nothing(tmp_path: Path) -> None:
    clock = Clock()
    coord = _WindowCoord(clock)
    rig = _rig(
        tmp_path,
        binding=_binding(limits=StreamLimits(max_in_flight=4, rate_per_min=2, max_attempts=2)),
        coord=coord,
    )
    rig.clock.t = clock.t
    coord.clock = rig.clock
    violations = _guard(rig)
    for x in range(10):
        rig.consumer.add(0, _v({"x": x}))
        rig.consumer.add(1, _v({"x": 100 + x}))
    rig.consumer.assign([0, 1])
    rig.settle(rounds=800, advance=2.0)
    shed = rig.ingress.stats()["proj/s1"]["shed"]["rate"]
    assert shed > 20  # the replay really was throttled, many times over
    assert rig.sink.records == []  # …and never dead-lettered a healthy message
    assert violations == []
    assert all(rig.client.count(x) == 1 for x in [*range(10), *range(100, 110)])
    assert rig.consumer.stores(0)[-1] == 10 and rig.consumer.stores(1)[-1] == 10


def test_the_ingress_stamps_shed_reason_on_its_own_sheds_only(tmp_path: Path) -> None:
    """The backpressure signal is explicit: ``shed_reason`` is set by the ingress's in-flight
    and rate sheds, and by nothing else — not by an upstream 503 passing through it."""
    from examlops.dataplane.streams.client import classify_response

    class _NoRate(_Coord):
        def allow(self, bucket: str, limit: int, window_s: float) -> bool:
            return False

    registry = ModelSchemaRegistry(tmp_path)
    binding = _binding(limits=StreamLimits(max_in_flight=1, rate_per_min=5))
    req = StreamRequest("s1", "", "", {"x": 1})

    ingress = StreamIngress(ScriptedClient(), _Spool(), None, registry, _Coord())
    held = ingress._semaphore("proj/s1", 1)
    assert held.acquire(blocking=False)  # another connector holds the stream's only permit
    try:
        in_flight = ingress.handle(binding, req)
    finally:
        held.release()
    assert in_flight.outcome == "overloaded" and in_flight.shed_reason == "in_flight"
    assert ks.is_local_shed(in_flight)

    rate = StreamIngress(ScriptedClient(), _Spool(), None, registry, _NoRate()).handle(binding, req)
    assert rate.outcome == "overloaded" and rate.shed_reason == "rate" and ks.is_local_shed(rate)

    upstream_503 = classify_response(503, None, {"retry-after": "2"})
    passed = StreamIngress(
        ScriptedClient({1: [upstream_503]}), _Spool(), None, registry, _Coord()
    ).handle(_binding(), req)
    assert passed.outcome == "overloaded" and passed.shed_reason is None
    assert not ks.is_local_shed(passed)
    assert not ks.is_local_shed(bare_overloaded())  # no upstream key, no reason: not a shed


def test_a_rate_shed_is_paced_without_spending_an_attempt(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        binding=_binding(limits=StreamLimits(max_in_flight=4, max_attempts=2)),
        sheds={1: 5},
        shed_retry_after=2.0,
    )
    violations = _guard(rig)
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    assert rig.served.shed[1] == 1 and rig.consumer.paused == {0}
    assert rig.statuses[-1][0] == "retrying" and "shed" in (rig.statuses[-1][1] or "")
    rig.clock.advance(1.9)
    rig.steps()
    assert rig.served.shed[1] == 1  # paced by the shed's retry_after
    rig.settle(advance=2.0)
    assert rig.served.shed[1] == 5 and rig.client.count(1) == 1  # 5 sheds > max_attempts=2
    assert rig.sink.records == [] and violations == []
    assert rig.consumer.stores(0) == [1]


def test_an_overloaded_without_a_shed_reason_is_dead_lettered_after_max_attempts(
    tmp_path: Path,
) -> None:
    """Fail safe: an ``overloaded`` that carries no ``shed_reason`` — even one with no
    ``upstream`` key either — spends attempts, and is dead-lettered, never paced forever."""
    rig = _rig(tmp_path, script={1: [bare_overloaded()]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.settle(advance=1.0)
    assert rig.client.count(1) == 3  # max_attempts
    (dead,) = rig.sink.records
    assert dead["reason"] == "retries_exhausted" and "overloaded after 3 attempts" in dead["error"]
    assert rig.consumer.stores(0) == [1]


def test_an_upstream_overload_spends_attempts(tmp_path: Path) -> None:
    rig = _rig(tmp_path, script={1: [fail("overloaded", retry_after=0.5)]})
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.settle(advance=1.0)
    assert rig.client.count(1) == 3  # max_attempts
    assert [r["reason"] for r in rig.sink.records] == ["retries_exhausted"]
    assert rig.consumer.stores(0) == [1]


def test_a_binding_paused_in_the_catalog_starts_paused_for_that_run_only(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, binding=_binding(state="paused"))
    assert rig.session.paused
    assert not rig.connector.is_paused(_binding())  # not remembered by the instance
    rig.connector._sessions[("proj", "s1")] = rig.session  # as run() registers it
    rig.connector.pause(_binding(name="other"))
    assert rig.session.paused  # another stream's pause does not touch it
    rig.connector.resume(_binding())
    assert not rig.session.paused


# ── message format ───────────────────────────────────────────────────────────


def test_envelope_and_bare_forms() -> None:
    env = ks.parse_value(
        _v({"payload": {"x": 1}, "alias": "Staging", "metadata": {"a": 1}, "extra": 2}),
        max_bytes=1000,
    )
    assert env == ({"x": 1}, "", "Staging", {"a": 1})
    bare = ks.parse_value(_v({"x": 1, "payload": 3}), max_bytes=1000)
    assert bare == ({"x": 1, "payload": 3}, "", "", {})  # a non-object payload key: bare
    assert ks.parse_value(_v({"x": 1}), max_bytes=1000) == ({"x": 1}, "", "", {})


def test_envelope_fields_reach_the_request(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(
        0,
        _v({"payload": {"x": 1}, "metadata": {"a": 1, "tenant": "evil"}}),
        key=b"job-9",
    )
    rig.consumer.add(0, _v({"x": 2, "payload": 3}))
    rig.consumer.assign([0])
    rig.steps()
    first, second = rig.client.calls
    assert first.payload == {"x": 1} and first.alias == "Production" and first.model == "JPCP"
    assert first.metadata == {"a": 1, "key": "job-9", "job_id": "job-9"}  # tenant dropped
    assert second.payload == {"x": 2, "payload": 3} and second.alias == "Production"


def test_a_message_naming_another_alias_is_dead_lettered_not_served(tmp_path: Path) -> None:
    """C1 through the Kafka path: the envelope's ``alias`` reaches the ingress, which refuses it
    as ``validation`` because the binding's alias is authoritative — so it is dead-lettered at
    once (a validation failure is not retryable) and never inferred against another version."""
    rig = _rig(tmp_path)
    rig.consumer.add(0, _v({"payload": {"x": 1}, "alias": "Staging"}))
    rig.consumer.assign([0])
    rig.settle(rounds=5)
    assert rig.client.calls == []
    (dead,) = rig.sink.records
    assert dead["reason"] == "validation"
    assert "bound to alias Production" in dead["error"]


def test_a_stream_that_opts_in_may_be_told_a_known_alias(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_binding(options={"allow_alias_override": True}))
    rig.consumer.add(0, _v({"payload": {"x": 1}, "alias": "Staging"}))
    rig.consumer.add(0, _v({"payload": {"x": 2}, "alias": "not-an-alias"}))
    rig.consumer.assign([0])
    rig.settle(rounds=5)
    (served,) = rig.client.calls
    assert served.alias == "Staging"
    assert [r["reason"] for r in rig.sink.records] == ["validation"]  # the unknown name still is


def test_key_and_headers_map_onto_the_request(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(
        0,
        _v({"payload": {"x": 1}, "metadata": {"job_id": "explicit"}}),
        key=b"job-7",
        headers=[("TraceParent", TRACEPARENT.encode()), ("idempotency-key", b"idem-1")],
    )
    rig.consumer.add(0, _v({"x": 2}), headers=[("traceparent", b"not-a-traceparent")])
    rig.consumer.assign([0])
    rig.steps()
    first, second = rig.client.calls
    assert first.traceparent == TRACEPARENT and first.idempotency_key == "idem-1"
    assert first.metadata["key"] == "job-7" and first.metadata["job_id"] == "explicit"
    assert second.traceparent is None and second.idempotency_key is None
    assert "key" not in second.metadata


def test_an_overlong_idempotency_key_is_dead_lettered(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(0, _v({"x": 1}), headers=[("idempotency-key", b"k" * 201)])
    rig.consumer.assign([0])
    rig.steps()
    assert [r["reason"] for r in rig.sink.records] == ["invalid_message"]
    assert rig.client.calls == [] and rig.consumer.stores(0) == [1]


def test_a_redelivered_idempotent_message_is_replied_and_stored(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    for _ in range(2):
        rig.consumer.add(0, _v({"x": 1}), headers=[("idempotency-key", b"same")])
    rig.consumer.assign([0])
    rig.steps()
    assert rig.producer is not None and len(rig.producer.to("replies")) == 2
    assert rig.consumer.stores(0)[-1] == 2


# ── rebalance, stop, errors ──────────────────────────────────────────────────


def test_revoke_commits_settled_offsets_before_the_partitions_go(tmp_path: Path) -> None:
    rig = _rig(tmp_path, executor=ManualExecutor())
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(1, _v({"x": 10}))
    rig.consumer.add(1, _v({"x": 11}))
    rig.consumer.assign([0, 1])
    rig.steps()
    assert len(rig.executor.jobs) == 3
    rig.executor.run(0)
    rig.steps()  # one offset settled and stored
    rig.executor.run(1)  # finished, but no step has seen it yet
    rig.consumer.revoke([0, 1])
    rig.steps(1)
    commits = rig.consumer.commits()
    assert len(commits) == 1
    pairs, asynchronous = commits[0]
    assert asynchronous is False and sorted(pairs) == [(0, 1), (1, 1)]
    commit_at = next(i for i, e in enumerate(rig.consumer.log) if e[0] == "commit")
    assert rig.consumer.log.index(("revoked", [0, 1])) > commit_at  # inside on_revoke
    stores_before = list(rig.consumer.log)
    rig.executor.run(2)  # late work for a revoked partition
    rig.steps()
    assert [e for e in rig.consumer.log if e[0] in ("store", "commit")] == [
        e for e in stores_before if e[0] in ("store", "commit")
    ]


def test_lost_partitions_are_forgotten_without_a_commit(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps()
    rig.consumer.lose([0])
    rig.steps()
    assert rig.consumer.commits() == [] and rig.session.stored_offsets() == {}


def test_stop_drains_flushes_commits_then_closes(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    rig.producer.hold = True  # the reply is still undelivered when the stop arrives
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.add(1, _v({"x": 2}))
    rig.consumer.assign([0, 1])
    rig.steps(3)
    rig.consumer.poll_hook = lambda c: rig.clock.advance(1.0)
    rig.session.shutdown()  # flush() delivers the held reports
    ((pairs, asynchronous),) = rig.consumer.commits()
    assert asynchronous is False and sorted(pairs) == [(0, 1), (1, 1)]
    assert rig.consumer.names()[-1] == "close"
    assert rig.statuses[-1] == ("stopped", None)


def test_stop_is_bounded_and_never_commits_an_unanswered_message(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    assert rig.producer is not None
    rig.producer.hold = rig.producer.stuck = True  # deliveries never come back
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    rig.steps(3)
    rig.consumer.poll_hook = lambda c: rig.clock.advance(1.0)
    rig.session.shutdown()
    assert rig.consumer.commits() == [] and rig.consumer.stores(0) == []
    assert rig.consumer.names()[-1] == "close"


def test_a_fatal_consumer_error_raises_after_committing_and_closing(tmp_path: Path) -> None:
    rig = _rig(tmp_path, start=False)
    rig.consumer.add(0, _v({"x": 1}))
    rig.consumer.assign([0])
    calls = {"n": 0}

    def hook(c: FakeConsumer) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            c.events.append(("error", FakeError("SASL authentication failed", fatal=True)))

    rig.consumer.poll_hook = hook
    with pytest.raises(DataplaneError, match="SASL authentication failed"):
        rig.session.run(threading.Event())
    assert rig.consumer.commits() == [([(0, 1)], False)]
    assert rig.consumer.names()[-1] == "close"
    assert rig.statuses[-1][0] == "error"


def test_a_transient_error_is_reported_then_cleared(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.consumer.assign([0])
    rig.consumer.events.append(("msg_error", FakeError("all brokers down")))
    rig.steps(2)
    assert rig.statuses[-1] == ("error", "kafka: all brokers down")
    rig.consumer.add(0, _v({"x": 1}))
    rig.steps(2)
    assert rig.statuses[-1] == ("running", None)
    assert rig.consumer.stores(0) == [1]


# ── concurrency: the loop never blocks, pause from another thread ────────────


def test_poll_keeps_heartbeating_while_workers_are_busy(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        executor=ManualExecutor(),
        binding=_binding(limits=StreamLimits(max_in_flight=2, max_attempts=3)),
    )
    for x in range(10):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.assign([0])
    polls = rig.consumer.polls
    rig.steps(20)
    assert rig.consumer.polls - polls == 20  # every iteration polled
    assert len(rig.executor.jobs) == 2 == rig.session.busy  # the pool is full
    assert ("pause", [0]) in rig.consumer.log  # flow control, not a blocked loop
    rig.settle(rounds=60)
    assert [rig.client.count(x) for x in range(10)] == [1] * 10  # each exactly once
    assert rig.consumer.stores(0)[-1] == 10
    assert rig.consumer.paused == set()


def test_max_in_flight_is_capped_at_32_workers(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_binding(limits=StreamLimits(max_in_flight=100)))
    assert rig.session.capacity == 32 and rig.executor.workers == 32


def test_pause_from_another_thread_pauses_the_assignment_and_keeps_polling(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    rig.consumer.assign([0, 1])
    rig.steps(2)
    worker = threading.Thread(target=rig.session.set_paused, args=(True,))
    worker.start()
    worker.join(timeout=5)
    rig.consumer.add(0, _v({"x": 1}))
    polls = rig.consumer.polls
    rig.steps(5)
    assert ("pause", [0, 1]) in rig.consumer.log
    assert rig.consumer.polls - polls == 5 and rig.client.calls == []
    assert rig.statuses[-1] == ("paused", None)
    rig.session.set_paused(False)
    rig.steps()
    assert ("resume", [0, 1]) in rig.consumer.log
    assert rig.client.count(1) == 1 and rig.consumer.stores(0) == [1]


# ── configuration ────────────────────────────────────────────────────────────


def test_consumer_group_includes_the_project(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(reply_topic="replies"))
    conf = rig.consumer.conf
    assert conf["group.id"] == "dataplane:proj:s1"
    assert conf["enable.auto.offset.store"] is False and conf["enable.auto.commit"] is True
    assert conf["auto.offset.reset"] == "earliest"
    assert conf["bootstrap.servers"] == "127.0.0.1:9092" and callable(conf["error_cb"])
    assert rig.producer is not None
    assert rig.producer.conf["enable.idempotence"] is True and rig.producer.conf["acks"] == "all"
    assert ks.consumer_group(_binding(project="")) == "dataplane:_global:s1"
    assert ks.consumer_group(_binding(project="a")) != ks.consumer_group(_binding(project="b"))


def test_start_latest_skips_the_history_of_a_new_group(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rig = _rig(tmp_path, binding=_with(start="latest"))
    assert rig.consumer.conf["auto.offset.reset"] == "earliest"  # out-of-range safety, always
    for x in range(5):
        rig.consumer.add(0, _v({"x": x}))  # history: never replayed
    for x in range(3):
        rig.consumer.add(1, _v({"x": 100 + x}))
    rig.consumer.assign([0, 1])
    rig.steps(3)
    assert ("assign_offsets", [(0, 5), (1, 3)]) in rig.consumer.log  # before any fetch
    rig.consumer.add(0, _v({"x": 5}))
    rig.consumer.add(1, _v({"x": 103}))
    rig.steps()
    assert sorted(req.payload["x"] for req in rig.client.calls) == [5, 103]
    assert rig.consumer.stores(0) == [5, 6] and rig.consumer.stores(1) == [3, 4]  # start, then
    starts = [r for r in caplog.records if "starts at the log end" in r.getMessage()]
    assert len(starts) == 1 and starts[0].levelno == logging.INFO


def test_start_latest_resumes_a_committed_partition_from_its_commit(tmp_path: Path) -> None:
    rig = _rig(tmp_path, binding=_with(start="latest"))
    for x in range(7):
        rig.consumer.add(0, _v({"x": x}))
    rig.consumer.committed_offsets[0] = 3  # the group already consumed 0-2
    rig.consumer.add(1, _v({"x": 100}))  # partition 1: no commit, so it starts at its end
    rig.consumer.assign([0, 1])
    rig.steps()
    assert sorted(req.payload["x"] for req in rig.client.calls) == [3, 4, 5, 6]
    assert rig.consumer.stores(0)[-1] == 7
    # the same group later: after a revoke commits, a re-assignment resumes from the commit
    rig.consumer.revoke([0, 1])
    rig.steps(1)
    assert rig.consumer.committed_offsets == {0: 7, 1: 1}  # partition 1's start is committed
    rig.consumer.add(0, _v({"x": 7}))
    rig.consumer.add(1, _v({"x": 101}))
    rig.consumer.assign([0, 1])
    rig.steps()
    assert sorted(req.payload["x"] for req in rig.client.calls) == [3, 4, 5, 6, 7, 101]


def test_start_earliest_begins_a_new_group_at_the_log_start(tmp_path: Path) -> None:
    rig = _rig(tmp_path)  # start: earliest (the default)
    for x in range(2, 6):
        rig.consumer.add(0, _v({"x": x}), offset=x)  # retention: the log starts at 2
    rig.consumer.assign([0])
    rig.steps()
    assert not any(e[0] == "assign_offsets" for e in rig.consumer.log)
    assert [req.payload["x"] for req in rig.client.calls] == [2, 3, 4, 5]
    assert rig.consumer.stores(0)[-1] == 6


def test_an_unreadable_committed_offset_never_starts_at_the_log_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R22-3: a per-partition error from committed() is "unknown", not "no commit": that
    partition starts as start=earliest would (a replay, never a skip); logged once."""
    caplog.set_level(logging.WARNING)
    rig = _rig(tmp_path, binding=_with(start="latest"))
    real = rig.consumer.committed

    def committed(tps: list[Any], timeout: float | None = None) -> list[Any]:
        out = real(tps, timeout)
        for tp in out:
            if tp.partition == 1:
                tp.error = FakeError("coordinator not available")
        return out

    rig.consumer.committed = committed  # type: ignore[method-assign]
    for x in range(3):
        rig.consumer.add(0, _v({"x": x}))
        rig.consumer.add(1, _v({"x": 100 + x}))
    rig.consumer.assign([0, 1])
    rig.steps()
    assert ("assign_offsets", [(0, 3), (1, -1001)]) in rig.consumer.log
    assert sorted(req.payload["x"] for req in rig.client.calls) == [100, 101, 102]
    rig.consumer.revoke([0, 1])
    rig.steps(1)
    rig.consumer.assign([0, 1])
    rig.steps(2)
    warnings = [r for r in caplog.records if "committed offset unknown" in r.getMessage()]
    assert len(warnings) == 1  # logged once


def test_envelope_rejected_is_public_and_the_old_name_still_works() -> None:
    with pytest.raises(ks.EnvelopeRejected) as caught:
        ks.parse_value(b"[1]", max_bytes=100)
    assert caught.value.reason == "invalid_message"
    assert ks._Reject is ks.EnvelopeRejected  # A8's push route imports the old name today


def test_no_producer_without_a_reply_or_dlq_topic(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert rig.producer is None


def test_sasl_credentials_reach_both_clients(tmp_path: Path) -> None:
    conn = {
        **CONN,
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "PLAIN",
        "sasl_username": "u",
        "secret": "pw-123",
    }
    rig = _rig(tmp_path, binding=_with(dlq_topic="dlq"), conn=conn)
    assert rig.consumer.conf["sasl.password"] == "pw-123"
    assert rig.producer is not None
    assert rig.producer.conf["sasl.password"] == "pw-123"
    assert rig.producer.conf["security.protocol"] == "SASL_SSL"


def test_the_bootstrap_host_is_egress_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with pytest.raises(EgressDenied):
        _rig(tmp_path)


@pytest.mark.parametrize(
    ("binding", "match"),
    [
        (_binding(connection=None), "needs a kafka Named Connection"),
        (_binding(address=""), "needs a topic"),
        (_with(reply_topic=TOPIC), "must differ"),
        (_with(dlq_topic=TOPIC), "must differ"),
        (_with(start="middle"), "options.start"),
        (_with(reply_topic=7), "topic name"),
    ],
)
def test_an_unusable_binding_is_a_spec_error(
    tmp_path: Path, binding: StreamBinding, match: str
) -> None:
    with pytest.raises(SpecError, match=match):
        _rig(tmp_path, binding=binding)


def test_a_connection_of_another_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="needs a kafka connection"):
        _rig(tmp_path, conn={"kind": "s3", "bootstrap_servers": "127.0.0.1:9092"})


# ── run() on real threads ────────────────────────────────────────────────────


def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    done = threading.Event()
    for _ in range(int(timeout / 0.005)):
        if cond():
            return True
        done.wait(0.005)
    return cond()


def test_run_serves_until_stopped_and_honours_connector_pause(tmp_path: Path) -> None:
    consumers: list[FakeConsumer] = []

    def consumer_factory(conf: dict[str, Any]) -> FakeConsumer:
        c = FakeConsumer(conf)
        c.wait = True
        for x in range(5):
            c.add(0, _v({"x": x}))
        c.assign([0])
        consumers.append(c)
        return c

    connector = ks.KafkaStreamConnector(
        dlq=RecordingSink(),
        consumer_factory=consumer_factory,
        resolve_connection=lambda name, project=None: dict(CONN),
        poll_timeout_s=0.005,
    )
    client = ScriptedClient()
    ingress = StreamIngress(client, _Spool(), None, ModelSchemaRegistry(tmp_path), _Coord())
    statuses: list[tuple[str, str | None]] = []
    stop = threading.Event()
    binding = _binding()
    thread = threading.Thread(
        target=connector.run, args=(binding, ingress, stop, lambda s, d: statuses.append((s, d)))
    )
    thread.start()
    try:
        assert _wait_for(lambda: bool(consumers) and 5 in consumers[0].stores(0))
        consumer = consumers[0]
        connector.pause(binding)
        assert _wait_for(lambda: consumer.paused == {0})
        assert connector.is_paused(binding) and ("paused", None) in statuses
        polls = consumer.polls
        assert _wait_for(lambda: consumer.polls > polls + 3)  # still heartbeating
        consumer.add(0, _v({"x": 99}))
        connector.resume(binding)
        assert _wait_for(lambda: 6 in consumer.stores(0))
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert client.count(99) == 1
    assert statuses[0] == ("starting", None) and statuses[-1] == ("stopped", None)
    assert consumer.commits()[-1] == ([(0, 6)], False) and consumer.names()[-1] == "close"
    assert connector.session(binding) is None


def test_run_reports_and_raises_when_the_binding_is_unusable(tmp_path: Path) -> None:
    connector = ks.KafkaStreamConnector(resolve_connection=lambda name, project=None: dict(CONN))
    statuses: list[tuple[str, str | None]] = []
    ingress = StreamIngress(
        ScriptedClient(), _Spool(), None, ModelSchemaRegistry(tmp_path), _Coord()
    )
    with pytest.raises(SpecError):
        connector.run(
            _binding(connection=None),
            ingress,
            threading.Event(),
            lambda s, d: statuses.append((s, d)),
        )
    assert statuses[0] == ("starting", None) and statuses[-1][0] == "error"
