"""Unit tests for the dataplane streaming telemetry seam (ADR 0130/0131, Plan 2, task A3).

Covers: EmbeddingStats' formula, the spool's drop-on-full/close/failure-counting/shim behaviour,
the offer()/close() race fix (I3), the close()-timeout/sink-ownership fix (I4), DbTelemetrySink's
row-writing (canonical model spelling, bridge-parity outcome gating, no per-inference audit row,
TelemetryWriteError-reported batch resilience), the on_baseline callback (I5), the write() shim's
per-record failure isolation (I6), and a Protocol-only contract suite any TelemetrySink can be run
through (I7).

Every test gets its own ``PLATFORM_DB`` via the autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

import pytest

from examlops.data import get_db, init_db
from examlops.dataplane.streams.telemetry import (
    DbTelemetrySink,
    EmbeddingStats,
    TelemetryRecord,
    TelemetrySpool,
    TelemetryWriteError,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _record(**overrides: object) -> TelemetryRecord:
    fields: dict[str, object] = {
        "event_id": "evt-1",
        "ts": 1_700_000_000.0,
        "stream": "s1",
        "connector": "http",
        "model": "JPCP",
        "alias": "Production",
        "outcome": "ok",
        "prediction": 1.5,
        "job_id": "job-1",
    }
    fields.update(overrides)
    return TelemetryRecord(**fields)  # type: ignore[arg-type]


class _InMemorySink:
    """A minimal reference TelemetrySink for the spool/shim tests."""

    def __init__(self) -> None:
        self.batches: list[list[TelemetryRecord]] = []
        self.closed = False

    def write_batch(self, records: list[TelemetryRecord]) -> None:
        self.batches.append(list(records))

    def close(self) -> None:
        self.closed = True

    @property
    def records(self) -> list[TelemetryRecord]:
        return [r for batch in self.batches for r in batch]


class _WriteOnlySink:
    """A sink offering only ``write`` — no ``write_batch`` — to exercise the shim."""

    def __init__(self) -> None:
        self.written: list[TelemetryRecord] = []
        self.closed = False

    def write(self, record: TelemetryRecord) -> None:
        self.written.append(record)

    def close(self) -> None:
        self.closed = True


class _BlockingSink:
    """Blocks the drainer thread on its first ``write_batch`` call until released, so a test can
    deterministically observe the spool full or a close() timeout (no sleeps needed)."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.batches: list[list[TelemetryRecord]] = []
        self._first = True
        self._lock = threading.Lock()
        self.closed = False

    def write_batch(self, records: list[TelemetryRecord]) -> None:
        with self._lock:
            first = self._first
            self._first = False
        if first:
            self.started.set()
            self.release.wait(timeout=5.0)
        self.batches.append(list(records))

    def close(self) -> None:
        self.closed = True


class _FailingSink:
    """Always raises (a plain exception, no ``.failed``) from write_batch; used to prove the
    spool falls back to counting the whole batch and survives a failure."""

    def __init__(self) -> None:
        self.calls = 0

    def write_batch(self, records: list[TelemetryRecord]) -> None:
        self.calls += 1
        raise RuntimeError("simulated sink failure")

    def close(self) -> None:
        pass


class _AlwaysFailingSink:
    """A stub sink whose write_batch always reports every record failed via
    :class:`TelemetryWriteError` — used only by the contract suite (I7) to prove that shape is
    recognised end to end."""

    def write_batch(self, records: list[TelemetryRecord]) -> None:
        raise TelemetryWriteError(failed=len(records), total=len(records))

    def close(self) -> None:
        pass


def _make_failing_db_sink(monkeypatch: pytest.MonkeyPatch) -> DbTelemetrySink:
    """A ``DbTelemetrySink`` whose drift-snapshot write always raises — used only by the contract
    suite (I7) to prove the production sink also reports failure through ``TelemetryWriteError``."""
    import examlops.data.drift as drift_mod

    def _always_fails(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure for the contract test")

    monkeypatch.setattr(drift_mod, "write_drift_snapshot", _always_fails)
    return DbTelemetrySink()


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``predicate`` with a short sleep, bounded by ``timeout``. Only used where no event or
    callback can signal completion directly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── EmbeddingStats ───────────────────────────────────────────────────────────


def test_embedding_stats_from_vector_matches_bridge_formula():
    vals = [1.0, 2.0, 3.0, 4.0]
    n = len(vals)
    expected_mean = sum(vals) / n
    expected_std = math.sqrt(sum((v - expected_mean) ** 2 for v in vals) / n)
    expected_norm = math.sqrt(sum(v * v for v in vals))

    stats = EmbeddingStats.from_vector(vals)

    assert stats.dim == n
    assert stats.mean == pytest.approx(expected_mean)
    assert stats.std == pytest.approx(expected_std)
    assert stats.norm == pytest.approx(expected_norm)


def test_embedding_stats_from_vector_single_value_has_zero_std():
    stats = EmbeddingStats.from_vector([5.0])
    assert stats.dim == 1
    assert stats.mean == 5.0
    assert stats.std == 0.0
    assert stats.norm == 5.0


def test_embedding_stats_from_vector_empty_is_all_zero():
    stats = EmbeddingStats.from_vector([])
    assert stats == EmbeddingStats(norm=0.0, mean=0.0, std=0.0, dim=0)


# ── TelemetrySpool: drop-on-full ─────────────────────────────────────────────


def test_spool_drops_on_full_and_counts_it():
    sink = _BlockingSink()
    drops: list[None] = []
    spool = TelemetrySpool(sink, maxsize=1, batch=1, on_drop=lambda: drops.append(None))
    try:
        assert spool.offer(_record(event_id="r1")) is True
        # Wait for the drainer to actually pick r1 off the queue and block inside write_batch —
        # deterministic via the sink's own event, no sleep-based guessing.
        assert sink.started.wait(timeout=2.0)
        # The queue is now empty (r1 was dequeued) so this fills the one slot back up.
        assert spool.offer(_record(event_id="r2")) is True
        # The queue is full again (maxsize=1) and the drainer is still blocked on r1: dropped.
        assert spool.offer(_record(event_id="r3")) is False
        stats = spool.stats()
        assert stats["dropped"] == 1
        assert stats["failed"] == 0
        assert drops == [None]
    finally:
        sink.release.set()
        spool.close(timeout=2.0)


def test_offer_after_close_returns_false_and_counts_as_dropped():
    sink = _InMemorySink()
    spool = TelemetrySpool(sink, maxsize=10, batch=10)
    spool.close(timeout=2.0)
    assert spool.offer(_record()) is False
    assert spool.stats()["dropped"] == 1


# ── TelemetrySpool: close flushes ────────────────────────────────────────────


def test_close_flushes_queued_records_then_closes_sink():
    sink = _InMemorySink()
    spool = TelemetrySpool(sink, maxsize=100, batch=100)
    for i in range(5):
        assert spool.offer(_record(event_id=f"r{i}")) is True
    spool.close(timeout=2.0)

    assert {r.event_id for r in sink.records} == {f"r{i}" for i in range(5)}
    assert sink.closed is True


def test_close_is_idempotent():
    sink = _InMemorySink()
    spool = TelemetrySpool(sink, maxsize=10, batch=10)
    spool.close(timeout=2.0)
    spool.close(timeout=2.0)  # must not raise, must not double-close the sink oddly
    assert sink.closed is True


# ── TelemetrySpool: I3 — offer()/close() race ────────────────────────────────


def test_offer_close_race_never_loses_a_record():
    """I3: the closed-check and the enqueue in ``offer()`` happen under one lock, so a record can
    never be accepted (``offer()`` returns ``True``) and then vanish. Every accepted record is
    written, and every refused offer is counted as dropped: ``dropped == offered - accepted`` and
    ``written == accepted`` must both hold exactly, even while ``close()`` runs concurrently with
    a burst of ``offer()`` calls from several threads.
    """
    sink = _InMemorySink()
    spool = TelemetrySpool(sink, maxsize=1000, batch=50)
    n_threads = 8
    per_thread = 50
    accepted = [0] * n_threads
    barrier = threading.Barrier(n_threads + 1)

    def _worker(idx: int) -> None:
        barrier.wait(timeout=5.0)
        count = 0
        for i in range(per_thread):
            if spool.offer(_record(event_id=f"race-{idx}-{i}")):
                count += 1
        accepted[idx] = count

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    barrier.wait(timeout=5.0)  # release every worker at (roughly) the same moment as close()
    spool.close(timeout=5.0)  # generous: an in-memory sink drains near-instantly
    for t in threads:
        t.join(timeout=5.0)

    offered = n_threads * per_thread
    total_accepted = sum(accepted)
    written = len(sink.records)
    dropped = spool.stats()["dropped"]

    assert dropped == offered - total_accepted
    assert written == total_accepted


# ── TelemetrySpool: I4 — close() timeout vs. sink ownership ──────────────────


def test_close_timeout_counts_queued_as_dropped_and_drainer_closes_sink_itself():
    """I4: if the drainer is still inside a flush past ``close()``'s timeout, ``close()`` must not
    race the sink. It counts whatever is still queued as dropped and returns; only the drainer
    itself closes the sink, and only after its own flush finishes.
    """
    sink = _BlockingSink()
    spool = TelemetrySpool(sink, maxsize=10, batch=1)
    try:
        assert spool.offer(_record(event_id="in-flight"))
        assert sink.started.wait(timeout=2.0)  # the drainer is now blocked inside write_batch
        assert spool.offer(_record(event_id="queued"))  # sits in the queue, undrained

        spool.close(timeout=0.2)  # far shorter than the block: this must time out

        assert spool.stats()["dropped"] == 1  # the still-queued record, counted on timeout
        assert sink.closed is False  # close() must not have touched the sink while it was busy
    finally:
        sink.release.set()
        # The drainer finishes its blocked flush and closes the sink itself.
        assert _wait_until(lambda: sink.closed is True, timeout=2.0)


# ── TelemetrySpool: failure counting ─────────────────────────────────────────


def test_failing_sink_write_is_counted_and_does_not_crash_the_drainer():
    sink = _FailingSink()
    failed_event = threading.Event()
    spool = TelemetrySpool(sink, maxsize=10, batch=1, on_fail=failed_event.set)
    try:
        assert spool.offer(_record(event_id="r1")) is True
        assert failed_event.wait(timeout=2.0)
        # _FailingSink raises a plain RuntimeError (no `.failed`): the spool falls back to
        # counting the whole batch (size 1 here) as failed.
        assert spool.stats()["failed"] == 1

        # The drainer thread must still be alive and processing after a failure.
        failed_event.clear()
        assert spool.offer(_record(event_id="r2")) is True
        assert failed_event.wait(timeout=2.0)
        assert spool.stats()["failed"] == 2
        assert sink.calls == 2
    finally:
        spool.close(timeout=2.0)


# ── TelemetrySpool: write() shim (I6) ─────────────────────────────────────────


def test_write_only_sink_is_called_once_per_record():
    sink = _WriteOnlySink()
    spool = TelemetrySpool(sink, maxsize=10, batch=10)
    for i in range(3):
        spool.offer(_record(event_id=f"r{i}"))
    spool.close(timeout=2.0)

    assert {r.event_id for r in sink.written} == {"r0", "r1", "r2"}
    assert sink.closed is True


def test_write_shim_counts_only_failed_records_and_continues():
    """I6: each record goes through its own ``try`` in the shim, so one bad record neither aborts
    the records after it nor gets the successful ones counted as failed."""

    class _PartiallyFailingSink:
        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, record: TelemetryRecord) -> None:
            if record.event_id == "bad":
                raise RuntimeError("simulated per-record failure")
            self.written.append(record.event_id)

        def close(self) -> None:
            pass

    sink = _PartiallyFailingSink()
    failed_event = threading.Event()
    spool = TelemetrySpool(sink, maxsize=10, batch=3, on_fail=failed_event.set)
    try:
        assert spool.offer(_record(event_id="good-1"))
        assert spool.offer(_record(event_id="bad"))
        assert spool.offer(_record(event_id="good-2"))
        assert failed_event.wait(timeout=2.0)
    finally:
        spool.close(timeout=2.0)

    # Whether the three records were flushed together or across separate batches, exactly one
    # ("bad") ever fails, and both good records are written regardless.
    assert set(sink.written) == {"good-1", "good-2"}
    assert spool.stats()["failed"] == 1


# ── DbTelemetrySink ───────────────────────────────────────────────────────────


def _audit_row_count() -> int:
    init_db()
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]


def _drift_snapshot_models() -> list[str]:
    with get_db() as conn:
        return [r["model"] for r in conn.execute("SELECT model FROM drift_snapshots").fetchall()]


def _input_snapshot_rows() -> list[dict]:
    with get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT model, alias, emb_norm, emb_mean, emb_std, job_id FROM input_snapshots"
            ).fetchall()
        ]


def test_db_sink_writes_drift_and_input_snapshots_for_ok_record():
    before_audit = _audit_row_count()
    embedding = EmbeddingStats.from_vector([1.0, 2.0, 3.0])
    record = _record(
        model="JPCP",
        alias="Production",
        outcome="ok",
        prediction=42.5,
        embedding=embedding,
        job_id="job-42",
    )

    sink = DbTelemetrySink()
    sink.write_batch([record])

    # Canonical model spelling stored unchanged — no case folding.
    assert _drift_snapshot_models() == ["JPCP"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT model, alias, prediction, job_id FROM drift_snapshots"
        ).fetchone()
    assert row["model"] == "JPCP"
    assert row["alias"] == "Production"
    assert row["prediction"] == pytest.approx(42.5)
    assert row["job_id"] == "job-42"

    input_rows = _input_snapshot_rows()
    assert len(input_rows) == 1
    assert input_rows[0]["model"] == "JPCP"
    assert input_rows[0]["emb_norm"] == pytest.approx(embedding.norm)
    assert input_rows[0]["emb_mean"] == pytest.approx(embedding.mean)
    assert input_rows[0]["emb_std"] == pytest.approx(embedding.std)

    # No per-inference audit row, ever.
    assert _audit_row_count() == before_audit == 0

    sink.close()


def test_db_sink_skips_drift_snapshot_for_non_ok_outcome():
    record = _record(model="MACK", alias="Staging", outcome="model", prediction=9.9, embedding=None)
    sink = DbTelemetrySink()
    sink.write_batch([record])

    assert _drift_snapshot_models() == []
    assert _input_snapshot_rows() == []
    assert _audit_row_count() == 0


def test_db_sink_skips_drift_snapshot_when_prediction_is_none():
    record = _record(model="JPCP", outcome="ok", prediction=None)
    sink = DbTelemetrySink()
    sink.write_batch([record])
    assert _drift_snapshot_models() == []


def test_db_sink_skips_all_snapshots_for_non_ok_outcome_even_with_embedding():
    """Bridge parity (I2a): the bridge only ever offers telemetry from the success path of
    ``_call_pipeline`` — a failure never reaches the spool at all. So a non-``ok`` record must not
    persist an input snapshot either, even when it carries a real embedding."""
    embedding = EmbeddingStats.from_vector([0.1, 0.2, 0.3, 0.4])
    record = _record(model="MACK", alias="Canary", outcome="model", embedding=embedding)
    sink = DbTelemetrySink()
    sink.write_batch([record])

    assert _drift_snapshot_models() == []
    assert _input_snapshot_rows() == []


def test_db_sink_skips_input_snapshot_for_zero_dim_embedding():
    """Bridge parity (I2b): an empty embedding (``dim == 0``) must never persist an all-zero
    row — the bridge's ``if embedding:`` skips an empty vector entirely."""
    record = _record(
        model="JPCP", outcome="ok", prediction=1.0, embedding=EmbeddingStats.from_vector([])
    )
    sink = DbTelemetrySink()
    sink.write_batch([record])

    assert _input_snapshot_rows() == []
    # The drift write is gated separately (on prediction), so it still happens.
    assert _drift_snapshot_models() == ["JPCP"]


def test_db_sink_batch_survives_one_bad_record_and_reports_failure_count(monkeypatch):
    """I1: a failure writing one record's drift snapshot must not stop the rest of the batch, and
    the failure must be visible — ``DbTelemetrySink`` raises ``TelemetryWriteError`` naming
    exactly how many failed, and a spool running the same sink increments ``stats()["failed"]``
    by that count, not by the whole batch size."""
    import examlops.data.drift as drift_mod

    original = drift_mod.write_drift_snapshot

    def _flaky(model, alias, prediction, job_id):
        if model == "BAD":
            raise RuntimeError("simulated per-record failure")
        return original(model, alias, prediction, job_id)

    monkeypatch.setattr(drift_mod, "write_drift_snapshot", _flaky)

    records = [
        _record(event_id="bad", model="BAD", outcome="ok", prediction=1.0),
        _record(event_id="good", model="JPCP", outcome="ok", prediction=2.0),
    ]

    # Direct call: both records are attempted, the good one lands, and exactly one failure (not
    # the whole batch of two) is reported.
    sink = DbTelemetrySink()
    with pytest.raises(TelemetryWriteError) as exc_info:
        sink.write_batch(records)
    assert exc_info.value.failed == 1
    assert exc_info.value.total == 2
    assert _drift_snapshot_models() == ["JPCP"]

    # Through a spool: stats()["failed"] reflects the one failed record.
    failed_event = threading.Event()
    spool = TelemetrySpool(DbTelemetrySink(), maxsize=10, batch=2, on_fail=failed_event.set)
    try:
        assert spool.offer(records[0])
        assert spool.offer(records[1])
        assert failed_event.wait(timeout=2.0)
    finally:
        spool.close(timeout=2.0)
    assert spool.stats()["failed"] == 1
    assert _drift_snapshot_models() == ["JPCP", "JPCP"]


def test_db_sink_close_is_idempotent():
    sink = DbTelemetrySink()
    sink.close()
    second_close_raised = False
    try:
        sink.close()
    except Exception:
        second_close_raised = True
    assert second_close_raised is False


def test_db_sink_on_baseline_callback_receives_stats_dict():
    """I5: DbTelemetrySink touches no metrics registry itself — a baseline refresh is handed to
    the caller's ``on_baseline(model, stats)`` callback, unmodified, straight from
    ``examlops.data.drift.get_input_baseline``."""
    from examlops.data.drift import set_input_baseline

    init_db()  # set_input_baseline, unlike most drift.py helpers, does not call this itself
    model = "BASELINE-TEST"  # distinct from other tests' models: the TTL cache is process-global
    set_input_baseline(model, {"norm_mean": 1.0, "mean_mean": 0.5, "std_mean": 0.1})

    calls: list[tuple[str, dict]] = []
    sink = DbTelemetrySink(on_baseline=lambda m, stats: calls.append((m, stats)))
    record = _record(
        model=model, outcome="ok", embedding=EmbeddingStats.from_vector([1.0, 2.0, 3.0])
    )
    sink.write_batch([record])

    assert len(calls) == 1
    called_model, stats = calls[0]
    assert called_model == model
    assert stats["norm_mean"] == pytest.approx(1.0)
    assert stats["mean_mean"] == pytest.approx(0.5)
    assert stats["std_mean"] == pytest.approx(0.1)


# ── End-to-end: spool + DbTelemetrySink ──────────────────────────────────────


def test_spool_with_db_sink_end_to_end():
    spool = TelemetrySpool(DbTelemetrySink(), maxsize=100, batch=10)
    try:
        for i in range(3):
            assert spool.offer(_record(event_id=f"e2e-{i}", model="JPCP", prediction=float(i)))
    finally:
        spool.close(timeout=2.0)

    assert _wait_until(lambda: len(_drift_snapshot_models()) == 3)
    assert _drift_snapshot_models() == ["JPCP", "JPCP", "JPCP"]
    assert _audit_row_count() == 0


# ── Reusable contract suite (I7) ───────────────────────────────────────────────


def _db_read_back(record: TelemetryRecord) -> bool:
    return record.model in _drift_snapshot_models()


def assert_sink_contract(
    make_sink: Callable[[], object],
    make_failing_sink: Callable[[], object],
    *,
    read_back: Callable[[TelemetryRecord], bool] | None = None,
) -> None:
    """Any :class:`TelemetrySink` — the Protocol only, ``write_batch`` + ``close`` — must satisfy
    this: an empty batch is a no-op, a real non-empty batch is accepted, ``close()`` is callable a
    second time without raising, and a sink built to fail reports through
    :class:`TelemetryWriteError` carrying how many of the batch failed.

    ``read_back(record)``, if given, proves persistence externally (used for ``DbTelemetrySink``;
    an in-memory stub has no external store to check, so it is omitted there).
    """
    sink = make_sink()

    # An empty batch is a no-op: must not raise.
    sink.write_batch([])

    # A real, non-empty batch is accepted without raising.
    record = _record(event_id="contract-ok", model="CONTRACT-OK", outcome="ok", prediction=1.0)
    sink.write_batch([record])
    if read_back is not None:
        assert read_back(record), "a successfully-written record must be observable afterwards"

    # close() is idempotent: a second call must not raise.
    sink.close()
    second_close_raised = False
    try:
        sink.close()
    except Exception:
        second_close_raised = True
    assert second_close_raised is False, "close() must be callable a second time without raising"

    # A failing sink reports through TelemetryWriteError, carrying how many of the batch failed.
    failing_sink = make_failing_sink()
    failing_record = _record(
        event_id="contract-fail", model="CONTRACT-FAIL", outcome="ok", prediction=1.0
    )
    with pytest.raises(TelemetryWriteError) as exc_info:
        failing_sink.write_batch([failing_record])
    assert exc_info.value.failed == 1
    assert exc_info.value.total == 1
    failing_sink.close()


def test_sink_contract_db(monkeypatch):
    assert_sink_contract(
        lambda: DbTelemetrySink(),
        lambda: _make_failing_db_sink(monkeypatch),
        read_back=_db_read_back,
    )


def test_sink_contract_in_memory():
    assert_sink_contract(lambda: _InMemorySink(), lambda: _AlwaysFailingSink())
