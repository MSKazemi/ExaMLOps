"""Bounded Kafka topic read (ADR 0130 §5): assign partitions, fixed end offsets, never commit."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import redact
from examlops.dataplane.types import (
    DataplaneError,
    Limits,
    Probe,
    SpecError,
    TableBatch,
    TableInfo,
    Watermark,
)

_CHUNK = 10_000


def _default_factory(conf: dict[str, Any]) -> Any:
    from confluent_kafka import Consumer

    return Consumer(conf)


_consumer_factory = _default_factory


def _tp(topic: str, partition: int, offset: int = -1) -> Any:
    try:
        from confluent_kafka import TopicPartition

        return TopicPartition(topic, partition, offset)
    except Exception:  # the fake consumer in tests accepts any object with these attributes
        return type("TP", (), {"topic": topic, "partition": partition, "offset": offset})()


def _conf(conn: dict[str, Any] | None, spec: dict[str, Any] | None) -> dict[str, Any]:
    cfg = conn or {}
    if not cfg.get("bootstrap_servers"):
        raise SpecError("kafka connection needs config.bootstrap_servers")
    conf: dict[str, Any] = {
        "bootstrap.servers": cfg["bootstrap_servers"],
        "group.id": f"examlops-dataplane-{uuid.uuid4().hex[:12]}",  # librdkafka requires one; never committed
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "enable.partition.eof": False,
        "auto.offset.reset": "earliest",
    }
    if cfg.get("security_protocol"):
        conf["security.protocol"] = cfg["security_protocol"]
    if cfg.get("sasl_mechanism"):
        conf["sasl.mechanism"] = cfg["sasl_mechanism"]
        conf["sasl.username"] = cfg.get("sasl_username", "")
        conf["sasl.password"] = cfg.get("secret", "")
    return conf


def _ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


class KafkaConnector(BaseConnector):
    kind = "kafka"
    connection_kinds = ("kafka",)
    extra = "dataplane-kafka"
    requires = ("confluent_kafka", "pyarrow")
    required_spec = ("topic",)
    supports_incremental = True

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        try:
            consumer = _consumer_factory(_conf(conn, spec))
            try:
                topics = consumer.list_topics(timeout=5).topics
            finally:
                consumer.close()
            return Probe(True, f"{len(topics)} topics visible")
        except Exception as exc:
            return Probe(
                False,
                redact(f"{type(exc).__name__}: {exc}", secrets=[(conn or {}).get("secret") or ""]),
            )

    def discover(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> list[TableInfo]:
        consumer = _consumer_factory(_conf(conn, spec))
        try:
            return [TableInfo(t) for t in sorted(consumer.list_topics(timeout=5).topics)[:500]]
        finally:
            consumer.close()

    def _bounds(
        self,
        consumer: Any,
        topic: str,
        spec: dict[str, Any],
        since: Watermark | None,
    ) -> dict[int, tuple[int, int]]:
        meta = consumer.list_topics(topic, timeout=10).topics.get(topic)
        if meta is None:
            raise SpecError(f"topic {topic!r} does not exist")
        bounds: dict[int, tuple[int, int]] = {}
        start, end = spec.get("start", "earliest"), spec.get("end", "latest-at-start")
        for p in sorted(meta.partitions):
            low, high = consumer.get_watermark_offsets(_tp(topic, p), timeout=10)
            lo, hi = low, high
            if since and str(p) in (since.get("offsets") or {}):
                lo = max(low, int(since["offsets"][str(p)]))
            elif start == "latest":
                lo = high
            elif isinstance(start, dict) and "offset" in start:
                lo = max(low, int(start["offset"]))
            elif isinstance(start, dict) and "timestamp" in start:
                lo = consumer.offsets_for_times(
                    [_tp(topic, p, _ms(start["timestamp"]))], timeout=10
                )[0].offset
                lo = high if lo < 0 else lo
            if isinstance(end, dict) and "offset" in end:
                hi = min(high, int(end["offset"]))
            elif isinstance(end, dict) and "timestamp" in end:
                hi = consumer.offsets_for_times([_tp(topic, p, _ms(end["timestamp"]))], timeout=10)[
                    0
                ].offset
                hi = high if hi < 0 else hi
            bounds[p] = (lo, hi)
        return bounds

    def read(
        self,
        conn: dict[str, Any] | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        import pyarrow as pa

        topic = str(spec["topic"])
        table = str(spec.get("table") or topic)
        raw = spec.get("value_format", "json") == "raw"
        skip_bad = spec.get("on_decode_error", "fail") == "skip"
        max_records = spec.get("max_records")
        max_rows = limits.max_rows
        timeout = float(spec.get("poll_timeout_s") or 5.0)
        # A limit of exactly 0 means "read nothing" — `if max_rows:` would wrongly treat that
        # as "no limit" (0 is falsy), and even a correct `>= 0` check would still assign/consume
        # at least one poll before finding out. Short-circuit before touching the broker at all.
        if (max_rows is not None and max_rows == 0) or (
            max_records is not None and int(max_records) == 0
        ):
            return
        consumer = _consumer_factory(_conf(conn, spec))
        try:
            bounds = self._bounds(consumer, topic, spec, since)
            pending = {p: lo for p, (lo, hi) in bounds.items() if lo < hi}
            offsets = {str(p): lo for p, (lo, _hi) in bounds.items()}
            if not pending:
                return
            consumer.assign([_tp(topic, p, lo) for p, lo in pending.items()])
            buffer: list[dict[str, Any]] = []
            total = 0
            while pending:
                msgs = consumer.consume(num_messages=_CHUNK, timeout=timeout)
                if not msgs:
                    break  # nothing arrived within the poll timeout; the bound is best-effort
                stop = False
                for m in msgs:
                    if m.error():
                        raise DataplaneError(
                            redact(
                                f"kafka error on {topic}: {m.error()}",
                                secrets=[(conn or {}).get("secret") or ""],
                            )
                        )
                    p, o = m.partition(), m.offset()
                    if p not in pending or o >= bounds[p][1]:
                        continue
                    offsets[str(p)] = o + 1
                    if o + 1 >= bounds[p][1]:
                        pending.pop(p, None)
                    row: dict[str, Any] = {
                        "_partition": p,
                        "_offset": o,
                        "_timestamp": m.timestamp()[1],
                        "_key": (m.key() or b"").decode("utf-8", "replace"),
                    }
                    value = m.value() or b""
                    if raw:
                        row["value"] = value.decode("utf-8", "replace")
                    else:
                        try:
                            decoded = json.loads(value)
                        except ValueError:
                            if skip_bad:
                                continue
                            raise DataplaneError(
                                f"invalid JSON at {topic} partition {p} offset {o}"
                            ) from None
                        row.update(decoded if isinstance(decoded, dict) else {"value": decoded})
                    buffer.append(row)
                    total += 1
                    if max_records is not None and total >= int(max_records):
                        stop = True
                        break
                    if max_rows is not None and total >= max_rows:
                        stop = True
                        break
                if stop:
                    pending.clear()
                if len(buffer) >= _CHUNK or not pending:
                    if buffer:
                        yield TableBatch(
                            table,
                            pa.RecordBatch.from_pylist(buffer),
                            {"topic": topic, "offsets": dict(offsets)},
                        )
                        buffer = []
            if buffer:
                yield TableBatch(
                    table,
                    pa.RecordBatch.from_pylist(buffer),
                    {"topic": topic, "offsets": dict(offsets)},
                )
        finally:
            consumer.close()
