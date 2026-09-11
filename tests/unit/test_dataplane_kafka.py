"""ADR 0130 — kafka bounded read: fixed end offsets, no commits, incremental offsets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import kafka  # noqa: E402
from examlops.dataplane.types import DataplaneError, EgressDenied, Limits, SpecError  # noqa: E402


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
    # ADR 0130 §10 (fix KS): the bootstrap host is now egress-checked before a Consumer is built.
    # A loopback bootstrap is what every read/probe test in this file uses, so it must be
    # allow-listed for the pre-existing behaviour tests to keep exercising the fake consumer.
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    monkeypatch.setattr(kafka, "_consumer_factory", lambda conf: _FakeConsumer(conf, calls))
    return calls


CONN = {"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092"}


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
    conn = {"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092", "secret": "hunter2-secret"}
    with pytest.raises(DataplaneError) as exc_info:
        list(kafka.KafkaConnector().read(conn, {"topic": "jobs"}, None, Limits()))
    assert "hunter2-secret" not in str(exc_info.value)


# --- fix KS: kafka bootstrap.servers egress-checked (ADR 0130 §10) ------------------------------


def test_bootstrap_entries_parses_comma_separated_and_bracketed_ipv6():
    assert kafka._bootstrap_entries("a:1,b") == [("a", 1), ("b", 9092)]
    assert kafka._bootstrap_entries("[::1]:9092") == [("::1", 9092)]
    assert kafka._bootstrap_entries("[::1]") == [("::1", 9092)]
    assert kafka._bootstrap_entries("[::1]:9092, c:10") == [("::1", 9092), ("c", 10)]


def test_bootstrap_entries_rejects_a_malformed_entry():
    with pytest.raises(SpecError):
        kafka._bootstrap_entries("[::1")
    with pytest.raises(SpecError):
        kafka._bootstrap_entries("a:notaport")


def test_check_bootstrap_servers_checks_every_entry_and_defaults_the_port(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(kafka, "check_address", lambda host, port: calls.append((host, port)))
    kafka._check_bootstrap_servers("a,b:10")
    assert calls == [("a", 9092), ("b", 10)]


def test_conf_refuses_a_platform_internal_bootstrap_host(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with pytest.raises(EgressDenied, match="platform-internal"):
        kafka._conf({"kind": "kafka", "bootstrap_servers": "postgres:9092"}, None)


def test_conf_refuses_a_loopback_bootstrap_host(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with pytest.raises(EgressDenied):
        kafka._conf({"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092"}, None)


def test_conf_one_denied_entry_refuses_the_whole_bootstrap_list(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    with pytest.raises(EgressDenied, match="platform-internal"):
        kafka._conf({"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092,postgres:9092"}, None)


def test_conf_admits_an_explicitly_allow_listed_internal_name(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "localhost")
    conf = kafka._conf({"kind": "kafka", "bootstrap_servers": "localhost:9092"}, None)
    assert conf["bootstrap.servers"] == "localhost:9092"


def test_probe_is_refused_for_a_loopback_bootstrap_and_never_builds_a_consumer(log, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    probe = kafka.KafkaConnector().probe({"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092"})
    assert probe.ok is False
    assert log == []


def test_read_is_refused_for_an_internal_name_bootstrap_and_never_builds_a_consumer(
    log, monkeypatch
):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with pytest.raises(EgressDenied):
        list(
            kafka.KafkaConnector().read(
                {"kind": "kafka", "bootstrap_servers": "postgres:9092"},
                {"topic": "jobs"},
                None,
                Limits(),
            )
        )
    assert log == []


# --- fix KS round 1: Important 3 — a scheme-prefixed bootstrap entry and a DNS failure ----------


def test_bootstrap_entries_strips_a_known_listener_protocol_prefix():
    assert kafka._bootstrap_entries("PLAINTEXT://a:1") == [("a", 1)]
    assert kafka._bootstrap_entries("SSL://a") == [("a", 9092)]
    assert kafka._bootstrap_entries("sasl_ssl://[::1]:9093") == [("::1", 9093)]
    assert kafka._bootstrap_entries("SASL_PLAINTEXT://a:1,PLAINTEXT://b:2") == [
        ("a", 1),
        ("b", 2),
    ]


def test_bootstrap_entries_rejects_an_unknown_protocol_prefix():
    with pytest.raises(SpecError, match="unknown protocol"):
        kafka._bootstrap_entries("http://a:1")


def test_conf_admits_a_protocol_prefixed_allow_listed_host(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    conf = kafka._conf({"kind": "kafka", "bootstrap_servers": "PLAINTEXT://127.0.0.1:9092"}, None)
    assert conf["bootstrap.servers"] == "PLAINTEXT://127.0.0.1:9092"


def test_an_unresolvable_bootstrap_host_raises_egress_denied_not_a_raw_dns_error(monkeypatch):
    """fix KS round 1, Important 3: a real DNS failure (``socket.gaierror``) must never bubble out
    of the connector — ``safety.check_address`` maps it to ``EgressDenied`` (verified directly in
    ``test_dataplane_safety.py``); this proves the connector benefits without stubbing anything."""
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with pytest.raises(EgressDenied, match="did not resolve"):
        kafka._conf(
            {"kind": "kafka", "bootstrap_servers": "this-host-does-not-exist.invalid:9092"}, None
        )


# --- fix KS round 1: Important 5 — discover()/read() redact egress/spec failures ----------------


def test_discover_redacts_a_secret_from_an_egress_denial(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    conn = {"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092", "secret": "hunter2-secret"}
    with pytest.raises(EgressDenied) as exc_info:
        kafka.KafkaConnector().discover(conn, {"topic": "jobs"})
    assert "hunter2-secret" not in str(exc_info.value)


def test_read_routes_an_egress_denial_through_the_redact_helper(monkeypatch):
    """Proves the wiring, not just the absence of a leak: ``redact()`` is actually invoked on the
    discover()/read() path, the same one ``probe()`` uses."""
    calls: list[str] = []
    real_redact = kafka.redact

    def spy(text, **kw):
        calls.append(text)
        return real_redact(text, **kw)

    monkeypatch.setattr(kafka, "redact", spy)
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    conn = {"kind": "kafka", "bootstrap_servers": "127.0.0.1:9092"}
    with pytest.raises(EgressDenied):
        list(kafka.KafkaConnector().read(conn, {"topic": "jobs"}, None, Limits()))
    assert calls
