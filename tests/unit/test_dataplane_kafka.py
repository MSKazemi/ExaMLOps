"""ADR 0130 — kafka bounded read: fixed end offsets, no commits, incremental offsets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import kafka  # noqa: E402
from examlops.dataplane.types import DataplaneError, Limits  # noqa: E402


class _TP:
    def __init__(self, topic, partition, offset=-1):
        self.topic, self.partition, self.offset = topic, partition, offset


class _Msg:
    def __init__(self, p, o, value):
        self._p, self._o, self._v = p, o, value

    def error(self):
        return None

    def partition(self):
        return self._p

    def offset(self):
        return self._o

    def value(self):
        return self._v

    def key(self):
        return b"k"

    def timestamp(self):
        return (1, 1_700_000_000_000 + self._o)


class _FakeConsumer:
    """Two partitions; partition 0 has 3 messages, partition 1 has 2."""

    def __init__(self, conf, log):
        self.conf, self.log = conf, log
        self.data = {
            0: [json.dumps({"x": i}).encode() for i in range(3)],
            1: [json.dumps({"x": 10 + i}).encode() for i in range(2)],
        }
        self.pos: dict[int, int] = {}

    def list_topics(self, topic=None, timeout=None):
        class M:  # noqa: D401
            topics = {topic: type("T", (), {"partitions": {0: None, 1: None}})()}

        return M()

    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        return 0, len(self.data[tp.partition])

    def offsets_for_times(self, tps, timeout=None):
        return tps

    def assign(self, tps):
        self.log.append(("assign", [(t.partition, t.offset) for t in tps]))
        self.pos = {t.partition: t.offset for t in tps}

    def subscribe(self, *a, **k):  # must never be called
        raise AssertionError("bounded reads assign; they never subscribe")

    def commit(self, *a, **k):
        raise AssertionError("bounded reads never commit offsets")

    def consume(self, num_messages=1, timeout=None):
        out = []
        for p, o in list(self.pos.items()):
            if o < len(self.data[p]):
                out.append(_Msg(p, o, self.data[p][o]))
                self.pos[p] = o + 1
        return out[:num_messages]

    def close(self):
        self.log.append(("close",))


@pytest.fixture
def log(monkeypatch):
    calls: list = []
    monkeypatch.setattr(kafka, "_consumer_factory", lambda conf: _FakeConsumer(conf, calls))
    return calls


CONN = {"kind": "kafka", "bootstrap_servers": "broker:9092"}


def _read(spec, since=None):
    return list(kafka.KafkaConnector().read(CONN, spec, since, Limits()))


def test_reads_every_partition_up_to_the_end_fixed_at_start(log):
    rows = [r for tb in _read({"topic": "jobs"}) for r in tb.batch.to_pylist()]
    assert sorted(r["x"] for r in rows) == [0, 1, 2, 10, 11]
    assert {"_partition", "_offset", "_timestamp", "_key"} <= set(rows[0])
    assert log[-1] == ("close",)


def test_group_offsets_are_never_committed(log):
    conf = kafka._conf(CONN, None)
    assert conf["enable.auto.commit"] is False and conf["enable.auto.offset.store"] is False


def test_incremental_starts_from_the_watermark(log):
    first = _read({"topic": "jobs", "incremental": True})
    wm = first[-1].watermark
    assert wm == {"topic": "jobs", "offsets": {"0": 3, "1": 2}}
    assert _read({"topic": "jobs", "incremental": True}, wm) == []


def test_bad_json_fails_by_default_and_can_be_skipped(log, monkeypatch):
    real = _FakeConsumer.__init__

    def bad_init(self, conf, log_):
        real(self, conf, log_)
        self.data[0][1] = b"{not json"

    monkeypatch.setattr(_FakeConsumer, "__init__", bad_init)
    with pytest.raises(DataplaneError, match="partition 0 offset 1"):
        _read({"topic": "jobs"})
    rows = [
        r
        for tb in _read({"topic": "jobs", "on_decode_error": "skip"})
        for r in tb.batch.to_pylist()
    ]
    assert len(rows) == 4


def test_max_records_stops_and_leaves_watermark_at_last_consumed_offset(log):
    """max_records must cut the read short *and* the watermark must reflect only the
    offsets actually consumed (not the partition end), so a resumed pull picks up where
    the bounded read really stopped rather than skipping records."""
    batches = _read({"topic": "jobs", "max_records": 2})
    rows = [r for tb in batches for r in tb.batch.to_pylist()]
    assert len(rows) == 2
    wm = batches[-1].watermark
    # the fake consumer interleaves one message per pending partition per poll, so the
    # first poll yields (p0 offset 0, p1 offset 0) and max_records=2 stops right there:
    # each partition's watermark advances to next_offset 1, well short of its true end.
    assert wm == {"topic": "jobs", "offsets": {"0": 1, "1": 1}}
    assert wm["offsets"]["0"] < 3 and wm["offsets"]["1"] < 2  # not the partition end


def test_max_rows_limit_stops_consuming(log):
    batches = _read(
        {"topic": "jobs"},
    )
    # sanity: full read without limits pulls all 5 rows
    rows = [r for tb in batches for r in tb.batch.to_pylist()]
    assert len(rows) == 5

    limited = list(kafka.KafkaConnector().read(CONN, {"topic": "jobs"}, None, Limits(max_rows=3)))
    limited_rows = [r for tb in limited for r in tb.batch.to_pylist()]
    assert len(limited_rows) == 3


def test_max_rows_zero_reads_nothing(log):
    """Limits(max_rows=0) must mean "read nothing" — a falsy-0 check would wrongly read
    everything, and even a correct `>= 0` check inside the poll loop would still assign a
    consumer and take one poll before giving up. Nothing should touch the broker at all."""
    batches = list(kafka.KafkaConnector().read(CONN, {"topic": "jobs"}, None, Limits(max_rows=0)))
    assert batches == []
    assert log == []  # no consumer was even constructed: no assign, no close


def test_max_records_zero_reads_nothing(log):
    batches = _read({"topic": "jobs", "max_records": 0})
    assert batches == []
    assert log == []


class _ErrMsg:
    def __init__(self, err):
        self._err = err

    def error(self):
        return self._err


class _ErrConsumer(_FakeConsumer):
    """Every ``consume()`` call surfaces one broker error embedding the SASL password with no
    key=value shape, so only an explicit ``secrets=[...]`` pass to ``redact()`` (not the generic
    key/value heuristics) could possibly strip it."""

    def consume(self, num_messages=1, timeout=None):
        return [_ErrMsg("SaslAuthenticationRequiredError: rejected credential hunter2-secret")]


def test_broker_error_text_is_redacted(monkeypatch, log):
    monkeypatch.setattr(kafka, "_consumer_factory", lambda conf: _ErrConsumer(conf, log))
    conn = {"kind": "kafka", "bootstrap_servers": "broker:9092", "secret": "hunter2-secret"}
    with pytest.raises(DataplaneError) as exc_info:
        list(kafka.KafkaConnector().read(conn, {"topic": "jobs"}, None, Limits()))
    assert "hunter2-secret" not in str(exc_info.value)
