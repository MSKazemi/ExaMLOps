"""Stream dead-letter store (ADR 0131 §5, Plan 2 batch S1, task A7b, E6).

Covers ``examlops.dataplane.streams.dlq``'s database half and its storage helpers in
``examlops.data.dataplane``:

* the payload is stored only on opt-in (``options.dlq_store_payload``), at most 256 KiB, UTF-8
  only, and redacted (secrets + PII) — digest and size always describe the original bytes;
* the stored error is redacted and bounded;
* ``record()`` is idempotent on the Kafka origin (ruling R15) and raises on a database failure;
* replay re-offers the stored payload through the Kafka envelope parser, marks the row, audits,
  and refuses a missing payload, a double replay without ``force`` and a concurrent replay;
* purge and the scheduler's hourly retention prune delete and audit;
* tenancy: an id of another project reads like an unknown id;
* the dead-letter metric's ``reason`` label is bounded.

Fake credentials are assembled at runtime (never a literal key), so the gitleaks gate stays quiet.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from prometheus_client import REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.data import dataplane as catalog  # noqa: E402
from examlops.data import get_db  # noqa: E402
from examlops.data.audit import export_audit_events  # noqa: E402
from examlops.dataplane.service.scheduler import DLQ_PRUNE_INTERVAL_S, Scheduler  # noqa: E402
from examlops.dataplane.streams import dlq, metrics  # noqa: E402
from examlops.dataplane.streams.kafka_stream import (  # noqa: E402
    ANSWERED,
    REASON_EXPIRED,
    RETRYABLE,
    EnvelopeRejected,
)
from examlops.dataplane.streams.types import (  # noqa: E402
    InferenceResult,
    Outcome,
    StreamBinding,
    StreamLimits,
    StreamRequest,
)
from examlops.dataplane.types import SpecError  # noqa: E402

# Assembled at runtime: a literal would trip the repository's gitleaks gate.
_FAKE_TOKEN = "".join(["tk", "Zq", "9w", "Lr", "7x", "Pa", "4m", "Vn", "2c", "Jd", "8s"])
_FAKE_EMAIL = "".join(["jane.", "doe", "@", "example", ".org"])


def _binding(project: str = "p1", name: str = "orders", **options: Any) -> StreamBinding:
    return StreamBinding(
        project=project,
        name=name,
        connector="kafka",
        model="JPCP",
        alias="Production",
        address="orders-in",
        connection=None,
        options=dict(options),
        limits=StreamLimits(),
    )


def _origin(offset: int, *, topic: str = "orders-in", partition: int = 0) -> dict[str, Any]:
    return {"topic": topic, "partition": partition, "offset": offset}


def _record(
    sink: dlq.DbDeadLetterSink,
    binding: StreamBinding,
    *,
    payload: bytes | None = b'{"x": 1}',
    reason: str = "validation",
    error: str = "validation: missing field",
    attempts: int = 1,
    origin: dict[str, Any] | None = None,
) -> None:
    sink.record(
        binding,
        reason=reason,
        error=error,
        attempts=attempts,
        payload=payload,
        origin=_origin(0) if origin is None else origin,
    )


def _only_row(project: str = "p1") -> dict[str, Any]:
    rows = dlq.list_dead_letters(project)
    assert len(rows) == 1, rows
    full = dlq.get_dead_letter(project, rows[0]["id"])
    assert full is not None
    return full


def _age_rows(days: int, *, where: str = "1=1", args: tuple[Any, ...] = ()) -> None:
    """Backdate ``created_at`` of matching rows by ``days`` (UTC text, as the column holds)."""
    from datetime import UTC, datetime

    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            f"UPDATE dataplane_stream_dead_letters SET created_at=? WHERE {where}",  # noqa: S608
            (stamp, *args),
        )


def _audit(action: str) -> list[dict[str, Any]]:
    return [e for e in export_audit_events() if e["action"] == action]


def _details(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("details") or event.get("details_json") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _dead_letters_counted(project: str, stream: str, reason: str) -> float:
    labels = {"project": project, "stream": stream, "reason": reason}
    return REGISTRY.get_sample_value("dataplane_stream_dead_letters_total", labels) or 0.0


class _Ingress:
    def __init__(self, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.requests: list[tuple[StreamBinding, StreamRequest]] = []

    def handle(self, binding: StreamBinding, req: StreamRequest) -> InferenceResult:
        self.requests.append((binding, req))
        return InferenceResult(outcome=self.outcome, prediction=0.5)  # type: ignore[arg-type]


# ── what is stored ──────────────────────────────────────────────────────────────────────────


def test_no_payload_is_stored_by_default():
    raw = b'{"features": {"a": 1}}'
    _record(dlq.DbDeadLetterSink(), _binding(), payload=raw)

    row = _only_row()
    assert row["payload"] is None
    assert row["has_payload"] is False
    assert row["payload_encoding"] is None
    assert row["payload_truncated"] is False
    import hashlib

    assert row["sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["size"] == len(raw)
    assert row["origin"] == _origin(0)
    assert (row["origin_topic"], row["origin_partition"], row["origin_offset"]) == (
        "orders-in",
        0,
        0,
    )


def test_sink_default_opts_in_but_an_explicit_false_option_wins():
    sink = dlq.DbDeadLetterSink(dlq_store_payload_default=True)
    _record(sink, _binding(), payload=b'{"a": 1}', origin=_origin(1))
    _record(sink, _binding(dlq_store_payload="false"), payload=b'{"a": 2}', origin=_origin(2))

    rows = {r["origin_offset"]: r for r in dlq.list_dead_letters("p1")}
    assert rows[1]["has_payload"] is True
    assert rows[2]["has_payload"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        ("yes", True),
        (" TRUE ", True),
        (False, False),
        (0, False),
        (2, False),
        ("false", False),
        ("no", False),
        ("junk", False),
        (None, False),
    ],
)
def test_store_payload_option_is_a_strict_opt_in(value, expected):
    assert dlq.store_payload_enabled({"dlq_store_payload": value}, default=True) is expected


def test_absent_store_payload_option_falls_back_to_the_default():
    assert dlq.store_payload_enabled({}, default=False) is False
    assert dlq.store_payload_enabled({}, default=True) is True
    assert dlq.store_payload_enabled(None, default=True) is True


def test_opted_in_payload_is_stored_with_secrets_and_pii_redacted():
    message = {
        "payload": {
            "api_token": _FAKE_TOKEN,
            "note": f"contact {_FAKE_EMAIL} with password={_FAKE_TOKEN}",
            "header": f"Authorization: Bearer {_FAKE_TOKEN}",
            "phone": 5551234567,
            "embedding": [0.123456789012345, -1.5, 2.0],
            "max_tokens": 16,
        },
        "model": "JPCP",
    }
    raw = json.dumps(message).encode("utf-8")
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    stored = row["payload"]
    assert row["payload_encoding"] == dlq.ENCODING_UTF8
    assert _FAKE_TOKEN not in stored
    assert _FAKE_EMAIL not in stored
    assert "[redacted-email]" in stored
    doc = json.loads(stored)  # still JSON: the redaction walks values, not the raw text
    inner = doc["payload"]
    assert inner["api_token"] == "***"
    assert inner["phone"] == "[redacted-phone]"
    assert inner["embedding"] == [0.123456789012345, -1.5, 2.0]  # floats are never mangled
    # a secret-shaped key masks its whole subtree, numbers included (review finding C1)
    assert inner["max_tokens"] == "***"
    assert doc["model"] == "JPCP"
    # the digest and size describe the ORIGINAL bytes, not the redacted text
    import hashlib

    assert row["sha256"] == hashlib.sha256(raw).hexdigest()
    assert row["size"] == len(raw)


def test_a_secret_shaped_key_masks_its_whole_subtree():
    """Review finding C1: a list of credentials under a secret-shaped key survived redaction,
    because its elements were scrubbed as standalone strings and `redact()`'s patterns look for
    `key=value`-shaped text, not a bare opaque token."""
    other = _FAKE_TOKEN[::-1]
    message = {
        "api_keys": [_FAKE_TOKEN, other],
        "auth": {"tokens": [{"value": _FAKE_TOKEN}, {"value": other}]},
        "nested": {"credentials": [[_FAKE_TOKEN], {"deep": {"x": other}}]},
        "keep": "plain text",
    }
    raw = json.dumps(message).encode("utf-8")
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    stored = _only_row()["payload"]
    assert _FAKE_TOKEN not in stored
    assert other not in stored
    doc = json.loads(stored)
    assert doc["api_keys"] == "***"
    assert doc["auth"] == {"tokens": "***"}  # "auth" is not secret-shaped; "tokens" is
    assert doc["nested"] == {"credentials": "***"}
    assert doc["keep"] == "plain text"


def test_pii_is_redacted_everywhere_in_the_structure():
    message = {
        "contacts": [_FAKE_EMAIL, {"alt": f"write to {_FAKE_EMAIL}"}],
        _FAKE_EMAIL: "a key can be personal data too",
    }
    raw = json.dumps(message).encode("utf-8")
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    stored = _only_row()["payload"]
    assert _FAKE_EMAIL not in stored
    doc = json.loads(stored)
    assert doc["contacts"][0] == "[redacted-email]"
    assert "[redacted-email]" in doc["contacts"][1]["alt"]
    assert "[redacted-email]" in doc


def test_a_bare_top_level_list_is_redacted_element_by_element():
    raw = json.dumps([f"reach {_FAKE_EMAIL}", f"token={_FAKE_TOKEN}", {"api_key": _FAKE_TOKEN}])
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw.encode())

    stored = _only_row()["payload"]
    assert _FAKE_TOKEN not in stored
    assert _FAKE_EMAIL not in stored
    doc = json.loads(stored)
    assert doc[0] == "reach [redacted-email]"
    assert doc[1] == "token=***"
    assert doc[2] == {"api_key": "***"}


def _nested(levels: int, leaf: Any) -> str:
    """``levels`` nested single-key objects around ``leaf``, built as text so no Python recursion
    is involved in producing it."""
    return '{"a":' * levels + json.dumps(leaf) + "}" * levels


def test_a_payload_nested_past_the_cap_is_not_stored():
    """Re-review of C1: at ~20 000 levels — still far under 256 KiB, brackets being cheap — the
    structured walk cannot run, and the old fallback redacted by pattern, which does not know a
    key's shape and so let an array under a secret-shaped key through."""
    raw = _nested(20_000, {"api_keys": [_FAKE_TOKEN]}).encode()
    assert len(raw) < dlq.PAYLOAD_MAX_BYTES
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    assert row["payload"] is None
    assert row["has_payload"] is False
    assert row["payload_encoding"] == dlq.ENCODING_TOO_DEEP_DROPPED
    assert row["payload_truncated"] is False
    import hashlib

    assert row["sha256"] == hashlib.sha256(raw).hexdigest()  # metadata and origin are still kept
    assert row["size"] == len(raw)
    assert row["origin"] == _origin(0)
    assert _FAKE_TOKEN not in json.dumps(row, default=str)


def test_a_payload_just_inside_the_cap_is_stored_and_scrubbed():
    # 29 wrappers + the object + its array = 31 nested containers, one under the 32 cap
    raw = _nested(29, {"api_keys": [_FAKE_TOKEN], "note": _FAKE_EMAIL}).encode()
    assert dlq.json_depth_exceeds(json.loads(raw)) is False
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    assert row["payload_encoding"] == dlq.ENCODING_UTF8
    assert _FAKE_TOKEN not in row["payload"]
    assert _FAKE_EMAIL not in row["payload"]
    doc = json.loads(row["payload"])
    for _ in range(29):
        doc = doc["a"]
    assert doc == {"api_keys": "***", "note": "[redacted-email]"}


def test_depth_is_measured_without_recursing():
    """An explicit stack, so measuring a deep document is not itself what raises."""
    deep = json.loads(_nested(200, 1))  # 200 levels parses; 20 000 does not
    assert dlq.json_depth_exceeds(deep) is True
    assert dlq.json_depth_exceeds(deep, limit=200) is False
    assert dlq.json_depth_exceeds(json.loads(_nested(32, 1))) is False
    assert dlq.json_depth_exceeds(json.loads(_nested(33, 1))) is True
    assert dlq.json_depth_exceeds([[{"a": [1, 2]}]]) is False
    assert dlq.json_depth_exceeds("a scalar") is False


def test_non_json_text_payload_is_redacted_as_text():
    raw = f"not json at all, reach {_FAKE_EMAIL} token={_FAKE_TOKEN}".encode()
    _record(
        dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw, reason="not_json"
    )

    stored = _only_row()["payload"]
    assert stored is not None
    assert _FAKE_TOKEN not in stored
    assert _FAKE_EMAIL not in stored


def test_payload_over_256_kib_is_flagged_truncated_and_not_stored():
    raw = b'"' + b"a" * (dlq.PAYLOAD_MAX_BYTES - 1) + b'"'  # one byte over the cap
    assert len(raw) == dlq.PAYLOAD_MAX_BYTES + 1
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    assert row["payload"] is None
    assert row["payload_truncated"] is True
    assert row["size"] == len(raw)


def test_payload_of_exactly_256_kib_is_stored():
    raw = b'"' + b"a" * (dlq.PAYLOAD_MAX_BYTES - 2) + b'"'
    assert len(raw) == dlq.PAYLOAD_MAX_BYTES
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    assert row["payload_truncated"] is False
    assert row["payload"] is not None


def test_binary_payload_is_dropped_but_described():
    raw = b"\xff\xfe\x00\x01binary"
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=raw)

    row = _only_row()
    assert row["payload"] is None
    assert row["payload_encoding"] == dlq.ENCODING_BINARY_DROPPED
    assert row["size"] == len(raw)


def test_a_redaction_failure_stores_no_payload(monkeypatch):
    def boom(_text: str) -> str:
        raise RuntimeError("scanner down")

    monkeypatch.setattr(dlq, "redact_payload_text", boom)
    _record(dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True), payload=b'{"a": 1}')

    row = _only_row()
    assert row["payload"] is None
    assert row["payload_encoding"] == dlq.ENCODING_REDACTION_FAILED


def test_expired_dead_letter_without_payload_is_recorded():
    _record(
        dlq.DbDeadLetterSink(),
        _binding(dlq_store_payload=True),
        payload=None,
        reason=REASON_EXPIRED,
        error="offset 7 left the log",
        origin=_origin(7),
    )

    row = _only_row()
    assert (row["payload"], row["sha256"], row["size"]) == (None, None, 0)
    assert row["reason"] == REASON_EXPIRED


def test_stored_error_is_redacted_and_bounded():
    error = f"validation: token={_FAKE_TOKEN} from {_FAKE_EMAIL} " + "x" * 5000
    _record(dlq.DbDeadLetterSink(), _binding(), error=error)

    stored = _only_row()["error"]
    assert _FAKE_TOKEN not in stored
    assert _FAKE_EMAIL not in stored
    assert len(stored) <= dlq.STORED_ERROR_MAX_CHARS
    assert stored.startswith("validation: token=***")


def test_text_no_column_can_hold_never_fails_the_write():
    """A NUL (Postgres refuses it) or a lone surrogate (SQLite cannot encode it) would fail the
    write on every retry and stall the partition behind a poison message."""
    sink, binding = dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True)
    _record(
        sink, binding, payload=b"text\x00with a NUL", error="bad\x00 \ud800 end", origin=_origin(1)
    )
    _record(sink, binding, payload=b'{"note": "lone \\ud800 surrogate"}', origin=_origin(2))

    rows = {
        r["origin_offset"]: dlq.get_dead_letter("p1", r["id"]) for r in dlq.list_dead_letters("p1")
    }
    nul, surrogate = rows[1], rows[2]
    assert nul is not None and surrogate is not None
    assert nul["payload"] is None and nul["payload_encoding"] == dlq.ENCODING_BINARY_DROPPED
    assert "\x00" not in nul["error"] and nul["error"].startswith("bad? ")
    assert json.loads(surrogate["payload"]) == {"note": "lone \ud800 surrogate"}
    result = dlq.replay(surrogate["id"], actor="alice", ingress=_Ingress("ok"), binding=binding)
    assert result.outcome == "ok"


# ── idempotency and failure ─────────────────────────────────────────────────────────────────


def test_record_is_idempotent_on_the_origin():
    sink, binding = dlq.DbDeadLetterSink(), _binding()
    before = _dead_letters_counted("p1", "orders", "retries_exhausted")
    _record(sink, binding, reason="retries_exhausted", error="first", attempts=5)
    _record(sink, binding, reason="retries_exhausted", error="second", attempts=3)

    row = _only_row()
    assert row["attempts"] == 5  # the max, not the latest
    assert row["error"] == "second"  # not yet replayed: reason/error follow the latest record
    # counted once: the dedupe update is not a new dead letter
    assert _dead_letters_counted("p1", "orders", "retries_exhausted") == before + 1


def test_dedupe_keeps_reason_and_error_of_a_replayed_row():
    sink, binding = dlq.DbDeadLetterSink(), _binding(dlq_store_payload=True)
    _record(sink, binding, payload=b'{"a": 1}', reason="validation", error="bad", attempts=1)
    row = _only_row()
    dlq.replay(row["id"], actor="alice", ingress=_Ingress("ok"), binding=binding)

    _record(sink, binding, payload=None, reason=REASON_EXPIRED, error="gone", attempts=4)

    row = _only_row()
    assert (row["reason"], row["error"], row["attempts"]) == ("validation", "bad", 4)
    assert row["payload"] is not None  # payload columns are insert-only


def test_records_without_a_complete_origin_are_never_deduplicated():
    sink, binding = dlq.DbDeadLetterSink(), _binding()
    _record(sink, binding, origin={"request_id": "r1"})
    _record(sink, binding, origin={"request_id": "r1"})
    _record(sink, binding, origin={"topic": "orders-in", "partition": 0})  # no offset

    rows = dlq.list_dead_letters("p1")
    assert len(rows) == 3
    assert all(r["origin_offset"] is None for r in rows)


def test_same_offset_on_another_stream_or_project_is_a_different_dead_letter():
    sink = dlq.DbDeadLetterSink()
    _record(sink, _binding("p1", "orders"))
    _record(sink, _binding("p1", "billing"))
    _record(sink, _binding("p2", "orders"))

    assert len(dlq.list_dead_letters("p1")) == 2
    assert len(dlq.list_dead_letters("p2")) == 1


def test_record_raises_when_the_database_write_fails(monkeypatch):
    def down(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalog, "upsert_dead_letter", down)
    before = _dead_letters_counted("p1", "orders", "validation")

    with pytest.raises(sqlite3.OperationalError):
        _record(dlq.DbDeadLetterSink(), _binding())
    assert _dead_letters_counted("p1", "orders", "validation") == before


# ── metric ──────────────────────────────────────────────────────────────────────────────────


def test_dead_letter_metric_reason_is_bounded():
    before = _dead_letters_counted("p1", "orders", "other")
    _record(dlq.DbDeadLetterSink(), _binding(), reason="some-future-reason-" + "z" * 40)

    assert _dead_letters_counted("p1", "orders", "other") == before + 1
    assert metrics.dead_letter_reason("unheard-of") == "other"
    assert metrics.dead_letter_reason("not_json") == "not_json"


def test_metric_reason_set_covers_every_reason_a_connector_dead_letters_with():
    """Drift guard: a new dlq REASON_* or dead-lettering outcome must reach the label set."""
    reasons = {
        dlq.REASON_OVERSIZE,
        dlq.REASON_NOT_JSON,
        dlq.REASON_INVALID_MESSAGE,
        dlq.REASON_RETRIES_EXHAUSTED,
        REASON_EXPIRED,
    }
    dead_lettering_outcomes = set(get_args(Outcome)) - set(ANSWERED) - set(RETRYABLE)
    assert reasons | dead_lettering_outcomes <= metrics.DEAD_LETTER_REASONS


# ── reading and tenancy ─────────────────────────────────────────────────────────────────────


def test_get_of_another_projects_id_returns_none():
    _record(dlq.DbDeadLetterSink(), _binding("p1"), payload=b'{"a": 1}')
    row_id = dlq.list_dead_letters("p1")[0]["id"]

    assert dlq.get_dead_letter("p1", row_id) is not None
    assert dlq.get_dead_letter("p2", row_id) is None
    assert dlq.get_dead_letter("", row_id) is None
    assert dlq.get_dead_letter("p1", 999_999) is None
    assert dlq.get_dead_letter("p1", "not-an-id") is None
    assert dlq.list_dead_letters("p2") == []


def test_list_filters_by_stream_orders_newest_first_and_omits_payloads():
    sink = dlq.DbDeadLetterSink()
    for offset in range(3):
        _record(sink, _binding(dlq_store_payload=True), origin=_origin(offset))
    _record(sink, _binding(name="billing"), origin=_origin(9))

    rows = dlq.list_dead_letters("p1", stream="orders")
    assert [r["origin_offset"] for r in rows] == [2, 1, 0]
    assert all("payload" not in r and r["has_payload"] is True for r in rows)
    assert len(dlq.list_dead_letters("p1", limit=2)) == 2


# ── replay ──────────────────────────────────────────────────────────────────────────────────


def _stored_envelope(binding: StreamBinding, *, offset: int = 0) -> int:
    envelope = {
        "payload": {"features": {"a": 1}},
        "alias": "Staging",
        "metadata": {"job_id": "j-1", "tenant": "someone-else"},
    }
    _record(
        dlq.DbDeadLetterSink(),
        binding,
        payload=json.dumps(envelope).encode(),
        origin=_origin(offset),
    )
    return int(dlq.list_dead_letters(binding.project)[0]["id"])


def test_replay_success_marks_the_row_and_audits():
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)
    ingress = _Ingress("ok")

    result = dlq.replay(row_id, actor="alice", ingress=ingress, binding=binding)

    assert result.outcome == "ok"
    ((_b, req),) = ingress.requests
    assert req.payload == {"features": {"a": 1}}
    assert (req.stream, req.alias) == ("orders", "Staging")
    assert req.metadata == {"job_id": "j-1"}  # a message never sets the tenant
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_at"] and row["replayed_by"] == "alice"
    (event,) = _audit("dataplane_stream_dlq_replayed")
    assert event["actor"] == "alice"
    assert event["target"] == "p1/orders"
    details = _details(event)
    assert (details["id"], details["outcome"], details["replayed"]) == (row_id, "ok", True)
    assert "features" not in json.dumps(event, default=str)  # never the payload


def test_replay_with_a_model_outcome_also_marks_the_row():
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)

    dlq.replay(row_id, actor="alice", ingress=_Ingress("model"), binding=binding)

    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_by"] == "alice"


def test_replay_with_a_transient_outcome_leaves_the_row_replayable():
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)

    result = dlq.replay(row_id, actor="alice", ingress=_Ingress("transport"), binding=binding)
    assert result.outcome == "transport"
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_at"] is None
    assert _details(_audit("dataplane_stream_dlq_replayed")[0])["replayed"] is False

    # the claim was released: a second attempt runs
    assert dlq.replay(row_id, actor="bob", ingress=_Ingress("ok"), binding=binding).outcome == "ok"


def test_replay_without_a_stored_payload_is_refused():
    binding = _binding()  # no dlq_store_payload
    _record(dlq.DbDeadLetterSink(), binding)
    row_id = dlq.list_dead_letters("p1")[0]["id"]
    ingress = _Ingress()

    with pytest.raises(SpecError, match=r"has no stored payload; re-publish the original message"):
        dlq.replay(row_id, actor="alice", ingress=ingress, binding=binding)
    assert ingress.requests == []


def test_double_replay_is_refused_without_force():
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)
    ingress = _Ingress("ok")
    dlq.replay(row_id, actor="alice", ingress=ingress, binding=binding)

    with pytest.raises(SpecError, match="already replayed"):
        dlq.replay(row_id, actor="bob", ingress=ingress, binding=binding)
    assert len(ingress.requests) == 1

    dlq.replay(row_id, actor="bob", ingress=ingress, binding=binding, force=True)
    assert len(ingress.requests) == 2
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_by"] == "bob"


def test_a_replay_in_progress_blocks_another_until_its_claim_goes_stale():
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)
    far_past = "2000-01-01 00:00:00"
    assert catalog.claim_dead_letter_replay(
        "p1", row_id, "other", force=False, stale_before=far_past
    )

    with pytest.raises(SpecError, match="is being replayed"):
        dlq.replay(row_id, actor="alice", ingress=_Ingress(), binding=binding)

    with get_db() as conn:  # the other replayer died long ago
        conn.execute(
            "UPDATE dataplane_stream_dead_letters SET replay_claimed_at=? WHERE id=?",
            (far_past, row_id),
        )
    assert dlq.replay(row_id, actor="alice", ingress=_Ingress(), binding=binding).outcome == "ok"
    # the stale holder can no longer finish (or mark) the row
    assert not catalog.finish_dead_letter_replay("p1", row_id, "other", replayed_by="ghost")
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_by"] == "alice"


def test_a_replay_whose_claim_was_taken_over_says_so_and_never_claims_it_replayed():
    """Review finding I1: a slow replay used to write `dlq_replayed` with `replayed: true` even
    though its own write matched nothing, because a second replay had taken the stale claim."""
    binding = _binding(dlq_store_payload=True)
    row_id = _stored_envelope(binding)

    class _SlowIngress:
        """Runs long enough for its claim to go stale, and a second replay wins meanwhile."""

        def handle(self, _binding_: StreamBinding, _req: StreamRequest) -> InferenceResult:
            with get_db() as conn:  # what ten minutes of wall clock would do
                conn.execute(
                    "UPDATE dataplane_stream_dead_letters SET replay_claimed_at=? WHERE id=?",
                    ("2000-01-01 00:00:00", row_id),
                )
            dlq.replay(row_id, actor="bob", ingress=_Ingress("ok"), binding=binding)
            return InferenceResult(outcome="ok", prediction=0.5)

    result = dlq.replay(row_id, actor="alice", ingress=_SlowIngress(), binding=binding)

    assert result.outcome == "unexpected"
    assert result.body["error"] == dlq.REPLAY_CLAIM_LOST
    assert result.status == 409
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_by"] == "bob"  # the replay that owned the claim
    (lost,) = _audit("dataplane_stream_dlq_replay_lost")
    assert lost["actor"] == "alice"
    assert _details(lost)["id"] == row_id
    assert _details(lost)["replayed"] is False
    (replayed,) = _audit("dataplane_stream_dlq_replayed")  # only bob's counted
    assert replayed["actor"] == "bob"


def test_replay_across_projects_or_streams_reads_as_not_found():
    row_id = _stored_envelope(_binding("p1", dlq_store_payload=True))
    ingress = _Ingress()

    for other in (_binding("p2", dlq_store_payload=True), _binding("p1", "billing")):
        with pytest.raises(SpecError, match=rf"^dead letter {row_id} not found$"):
            dlq.replay(row_id, actor="mallory", ingress=ingress, binding=other)
    with pytest.raises(SpecError, match=r"^dead letter 424242 not found$"):
        dlq.replay(424242, actor="mallory", ingress=ingress, binding=_binding("p2"))
    assert ingress.requests == []


def test_replay_of_a_payload_the_parser_rejects_raises_and_releases_the_claim():
    binding = _binding(dlq_store_payload=True)
    _record(dlq.DbDeadLetterSink(), binding, payload=b"[1, 2, 3]", reason="invalid_message")
    row_id = dlq.list_dead_letters("p1")[0]["id"]
    ingress = _Ingress()

    for _ in range(2):  # the second attempt is rejected again, not refused as "being replayed"
        with pytest.raises(EnvelopeRejected) as info:
            dlq.replay(row_id, actor="alice", ingress=ingress, binding=binding)
        assert info.value.reason == "invalid_message"
    assert ingress.requests == []
    events = _audit("dataplane_stream_dlq_replayed")
    assert [_details(e)["outcome"] for e in events] == ["rejected", "rejected"]
    row = dlq.get_dead_letter("p1", row_id)
    assert row is not None and row["replayed_at"] is None


def test_a_failing_audit_write_never_replaces_the_real_failure(monkeypatch, caplog):
    """M6: the audit write lives in ``replay``'s ``finally``. An exception raised there used to
    REPLACE whatever the body raised — an ``EnvelopeRejected`` became an opaque database error,
    and the classifiable failure (which the route answers 422 for) was lost."""
    binding = _binding(dlq_store_payload=True)
    _record(dlq.DbDeadLetterSink(), binding, payload=b"[1, 2, 3]", reason="invalid_message")
    row_id = dlq.list_dead_letters("p1")[0]["id"]

    def _boom(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("audit table is locked")

    monkeypatch.setattr("examlops.data.audit.write_audit_event", _boom)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.dlq"):
        with pytest.raises(EnvelopeRejected) as info:  # still the real failure
            dlq.replay(row_id, actor="alice", ingress=_Ingress(), binding=binding)
    assert info.value.reason == "invalid_message"
    assert "could not audit the dead-letter replay" in caplog.text


def test_a_failing_claim_finish_never_replaces_the_real_failure(monkeypatch, caplog):
    """Re-review: M6 wrapped the audit write, and ``finish_dead_letter_replay`` sits one call up
    in the same ``finally`` — raising there replaced the body's exception just as surely."""
    binding = _binding(dlq_store_payload=True)
    _record(dlq.DbDeadLetterSink(), binding, payload=b"[1, 2, 3]", reason="invalid_message")
    row_id = dlq.list_dead_letters("p1")[0]["id"]

    def _boom(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalog, "finish_dead_letter_replay", _boom)
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.dlq"):
        with pytest.raises(EnvelopeRejected) as info:  # still the real failure
            dlq.replay(row_id, actor="alice", ingress=_Ingress(), binding=binding)
    assert info.value.reason == "invalid_message"
    assert "could not finish the dead-letter replay claim" in caplog.text
    # the claim was not finished, so the row is recorded as a lost claim, not a completion
    (lost,) = _audit("dataplane_stream_dlq_replay_lost")
    assert _details(lost)["replayed"] is False
    assert _audit("dataplane_stream_dlq_replayed") == []


def test_a_failing_claim_finish_still_returns_the_ingress_answer(monkeypatch, caplog):
    """The same, on the success path: the caller gets an answer, never a datastore exception."""
    binding = _binding(dlq_store_payload=True)
    _stored_envelope(binding)
    row_id = dlq.list_dead_letters("p1")[0]["id"]
    monkeypatch.setattr(
        catalog,
        "finish_dead_letter_replay",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with caplog.at_level(logging.WARNING, logger="examlops.dataplane.streams.dlq"):
        result = dlq.replay(row_id, actor="alice", ingress=_Ingress("ok"), binding=binding)
    assert result.outcome == "unexpected" and result.body["error"] == dlq.REPLAY_CLAIM_LOST
    assert "could not finish the dead-letter replay claim" in caplog.text


# ── purge and prune ─────────────────────────────────────────────────────────────────────────


def test_purge_deletes_old_rows_of_one_stream_counts_and_audits():
    sink = dlq.DbDeadLetterSink()
    for offset in range(3):
        _record(sink, _binding(), origin=_origin(offset))
    _record(sink, _binding(name="billing"), origin=_origin(0))
    _record(sink, _binding("p2"), origin=_origin(0))
    _age_rows(3)  # everything is three days old …
    _age_rows(0, where="origin_offset=?", args=(2,))  # … but offset 2 of every stream is new

    count = dlq.purge("p1", "orders", older_than=timedelta(days=1), actor="alice")

    assert count == 2
    assert [r["origin_offset"] for r in dlq.list_dead_letters("p1", stream="orders")] == [2]
    assert len(dlq.list_dead_letters("p1", stream="billing")) == 1  # another stream
    assert len(dlq.list_dead_letters("p2")) == 1  # another project
    (event,) = _audit("dataplane_stream_dlq_purged")
    assert (event["actor"], event["target"]) == ("alice", "p1/orders")
    assert _details(event)["count"] == 2


def test_purge_audits_even_when_nothing_matched_and_rejects_a_negative_age():
    assert dlq.purge("p1", None, older_than=timedelta(days=1), actor="alice") == 0
    assert _details(_audit("dataplane_stream_dlq_purged")[0])["count"] == 0
    with pytest.raises(ValueError):
        dlq.purge("p1", None, older_than=timedelta(seconds=-1), actor="alice")


def test_scheduler_tick_prunes_old_dead_letters_hourly_and_audits(monkeypatch):
    monkeypatch.delenv(dlq.RETENTION_ENV, raising=False)
    sink = dlq.DbDeadLetterSink()
    _record(sink, _binding("p1"), origin=_origin(0))
    _record(sink, _binding("p2"), origin=_origin(0))
    _record(sink, _binding("p1"), origin=_origin(1))
    _age_rows(8, where="origin_offset=?", args=(0,))  # past the 7-day default, both projects
    scheduler = Scheduler(interval_s=30)
    try:
        scheduler.tick(now=1_000_000.0)

        assert [r["origin_offset"] for r in dlq.list_dead_letters("p1")] == [1]
        assert dlq.list_dead_letters("p2") == []
        (event,) = _audit("dataplane_stream_dlq_pruned")
        assert _details(event) == {"count": 2, "retention_days": 7}

        # rate-limited: within the hour a newly-old row stays, and no run is audited
        _age_rows(8)
        scheduler.tick(now=1_000_000.0 + DLQ_PRUNE_INTERVAL_S - 1)
        assert len(dlq.list_dead_letters("p1")) == 1
        assert len(_audit("dataplane_stream_dlq_pruned")) == 1

        scheduler.tick(now=1_000_000.0 + DLQ_PRUNE_INTERVAL_S)
        assert dlq.list_dead_letters("p1") == []
        assert len(_audit("dataplane_stream_dlq_pruned")) == 2

        # a run that deletes nothing is not audited
        scheduler.tick(now=1_000_000.0 + 2 * DLQ_PRUNE_INTERVAL_S)
        assert len(_audit("dataplane_stream_dlq_pruned")) == 2
    finally:
        scheduler.stop(wait_s=0)


def test_prune_honours_the_retention_env(monkeypatch):
    _record(dlq.DbDeadLetterSink(), _binding(), origin=_origin(0))
    _age_rows(3)

    monkeypatch.setenv(dlq.RETENTION_ENV, "5")
    assert dlq.prune() == 0
    monkeypatch.setenv(dlq.RETENTION_ENV, "2")
    assert dlq.prune() == 1


@pytest.mark.parametrize("raw", ["0", "-3", "seven", "1.5"])
def test_invalid_retention_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv(dlq.RETENTION_ENV, raw)
    assert dlq.retention_days() == dlq.DEFAULT_RETENTION_DAYS


def test_a_prune_failure_warns_and_does_not_stop_the_tick(monkeypatch, caplog):
    def down(**_k: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalog, "prune_dead_letters", down)
    scheduler = Scheduler(interval_s=30)
    try:
        assert scheduler.tick(now=5_000.0) == []
        assert scheduler.prune_dead_letters(5_000.0 + 1) is None  # rate-limited, not retried
    finally:
        scheduler.stop(wait_s=0)
    warnings = [r for r in caplog.records if "could not prune stream dead letters" in r.message]
    assert len(warnings) == 1
