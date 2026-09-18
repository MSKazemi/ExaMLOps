"""Unit tests for the stream ingress's inference client (ADR 0130/0131, Plan 2, task A5).

Covers the one status mapping table (E4 + controller ruling R6) row by row and cause by cause,
every fallback, ``Retry-After`` parsing (delta-seconds and HTTP-date), the budget and
``traceparent`` headers, the budget-derived timeout, the per-thread ``httpx.Client``, and the
promise that the request body is never logged. Every call goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import email.utils
import logging
import threading
from typing import Any, get_args

import httpx
import pytest

from examlops.dataplane.streams.client import (
    BUDGET_HEADER,
    CALLER_STATUS,
    DEFAULT_INFERENCE_URL,
    INFER_PATH,
    INFERENCE_URL_ENV,
    MAX_CONNECTIONS_ENV,
    NO_CAUSE,
    OUTCOME_ANSWER,
    OUTCOME_TABLE,
    PUSH_SHED,
    RETRY_AFTER_MAX_S,
    RayPipelineClient,
    classify_response,
    effective_budget_ms,
    parse_retry_after,
)
from examlops.dataplane.streams.types import Outcome, StreamRequest

# ── helpers ──────────────────────────────────────────────────────────────────


def _req(**overrides: Any) -> StreamRequest:
    fields: dict[str, Any] = {
        "stream": "s1",
        "model": "JPCP",
        "alias": "Production",
        "payload": {"embedding": [0.1, 0.2]},
    }
    fields.update(overrides)
    return StreamRequest(**fields)


def _client(handler: Any) -> RayPipelineClient:
    return RayPipelineClient("http://pipeline.test", transport=httpx.MockTransport(handler))


def _answer(status: int, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text="Internal Server Error", headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return handler


def _body_for(status: int, cause: str | None) -> dict[str, Any]:
    if status == 200:
        return {"prediction": 1.5, "model_version": "3", "run_id": "r1"}
    if cause is None:
        return {"error": {422: "validation_error", 404: "model_not_found"}.get(status, "x")}
    body: dict[str, Any] = {"error": "inference_failed", "detail": "boom"}
    if cause != NO_CAUSE:
        body["cause"] = cause
    return body


# ── the mapping table: one test per row ──────────────────────────────────────


@pytest.mark.parametrize(("status", "cause"), sorted(OUTCOME_TABLE, key=str))
def test_every_table_row_maps_through_the_client(status: int, cause: str | None) -> None:
    expected = OUTCOME_TABLE[(status, cause)]
    result = _client(_answer(status, _body_for(status, cause))).infer(_req(), {"x": 1})
    assert result.outcome == expected
    assert result.status == CALLER_STATUS[expected]


def test_the_table_carries_every_ruled_row() -> None:
    """R6 verbatim: the rows the controller ruled, and nothing silently dropped."""
    assert OUTCOME_TABLE[(200, None)] == "ok"
    assert OUTCOME_TABLE[(422, None)] == "validation"
    assert OUTCOME_TABLE[(404, None)] == "not_found"
    assert OUTCOME_TABLE[(429, None)] == "overloaded"  # R9.1
    assert OUTCOME_TABLE[(502, None)] == "transport"  # R9.1
    assert OUTCOME_TABLE[(503, None)] == "overloaded"
    assert OUTCOME_TABLE[(504, None)] == "deadline"
    assert OUTCOME_TABLE[(500, NO_CAUSE)] == "model"
    assert OUTCOME_TABLE[(500, "model")] == "model"
    assert OUTCOME_TABLE[(500, "timeout")] == "deadline"
    for cause in ("transport", "replica_lost", "pipeline"):
        assert OUTCOME_TABLE[(500, cause)] == "transport"
    assert OUTCOME_TABLE[(500, "protocol")] == "unexpected"


def test_one_answer_table_serves_every_route() -> None:
    """I3: ``CALLER_STATUS`` (what a result carries, and what the dead-letter replay route
    answers) is the first column of ``OUTCOME_ANSWER`` (what the push route answers). They cannot
    disagree per outcome — they used to, 502 vs 500 on ``unexpected``."""
    outcomes = set(get_args(Outcome))
    assert set(OUTCOME_ANSWER) == outcomes == set(CALLER_STATUS)
    for outcome in outcomes:
        assert CALLER_STATUS[outcome] == OUTCOME_ANSWER[outcome][0]
    assert OUTCOME_ANSWER["unexpected"][0] == 500
    # every row is a complete problem document: status, type slug, title, detail
    for status, slug, title, detail in OUTCOME_ANSWER.values():
        assert 200 <= status < 600 and slug and title and detail
    # R18's one ruled departure, and the only one
    assert PUSH_SHED[0] == 429 and OUTCOME_ANSWER["overloaded"][0] == 503


def test_only_a_model_cause_or_no_cause_is_a_model_outcome() -> None:
    model_rows = {key for key, outcome in OUTCOME_TABLE.items() if outcome == "model"}
    assert model_rows == {(500, NO_CAUSE), (500, "model")}


def test_ok_carries_the_prediction_and_the_upstream_body() -> None:
    result = _client(_answer(200, _body_for(200, None))).infer(_req(), {})
    assert result.prediction == 1.5
    assert result.body["model_version"] == "3"


def test_a_model_failure_carries_a_bounded_detail() -> None:
    body = {"error": "inference_failed", "cause": "model", "detail": "x" * 1000}
    result = _client(_answer(500, body)).infer(_req(), {})
    assert result.outcome == "model"
    assert 0 < len(result.body["detail"]) <= 256


def test_an_upstream_422_body_is_never_forwarded() -> None:
    """A validation error body can echo the input — it must not reach the caller."""
    body = {"error": "validation_error", "detail": "embedding=[0.12345, secret-value]"}
    result = _client(_answer(422, body)).infer(_req(), {})
    assert result.outcome == "validation"
    assert "secret-value" not in repr(result.body)


# ── fallbacks: one test each ─────────────────────────────────────────────────


def test_an_unknown_cause_fails_safe_to_transport() -> None:
    body = {"error": "inference_failed", "cause": "cosmic_rays", "detail": "?"}
    assert _client(_answer(500, body)).infer(_req(), {}).outcome == "transport"


@pytest.mark.parametrize("body", [None, {"error": "something_else"}, ["not", "a", "dict"]])
def test_a_500_that_is_not_inference_failed_is_transport(body: Any) -> None:
    assert _client(_answer(500, body)).infer(_req(), {}).outcome == "transport"


@pytest.mark.parametrize("status", [400, 401, 403, 409, 501])
def test_any_other_status_is_unexpected(status: int) -> None:
    result = _client(_answer(status, {"error": "x"})).infer(_req(), {})
    assert result.outcome == "unexpected"
    assert result.status == 500  # I3: our own server error, and the same on every route


def test_a_502_from_a_gateway_is_transport() -> None:
    """R9.1: Envoy / Ray's proxy answer 502 when the upstream is gone — retryable, never drift."""
    result = _client(_answer(502, None)).infer(_req(), {})
    assert result.outcome == "transport"
    assert result.status == 502


def test_an_upstream_429_is_overloaded_and_keeps_retry_after() -> None:
    result = _client(_answer(429, {"error": "x"}, {"Retry-After": "12"})).infer(_req(), {})
    assert result.outcome == "overloaded"
    assert result.status == 503
    assert result.retry_after == 12.0


def test_a_200_without_a_json_object_is_unexpected() -> None:
    assert _client(_answer(200, [1, 2])).infer(_req(), {}).outcome == "unexpected"


@pytest.mark.parametrize(
    "exc_type", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError]
)
def test_network_errors_are_transport(exc_type: type[httpx.HTTPError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)

    result = _client(handler).infer(_req(), {})
    assert result.outcome == "transport"
    assert result.status == 502


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.WriteTimeout])
def test_a_read_or_write_timeout_under_a_budget_is_a_deadline(
    exc_type: type[httpx.TimeoutException],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("slow", request=request)

    result = _client(handler).infer(_req(deadline_ms=100), {})
    assert result.outcome == "deadline"
    assert result.status == 504


@pytest.mark.parametrize("exc_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
def test_a_connect_or_pool_timeout_is_transport_even_under_a_budget(
    exc_type: type[httpx.TimeoutException],
) -> None:
    """I2: these fire on their own short caps, long before a 30 s budget is spent — the upstream
    was unreachable (or our pool was full), which is retryable, not a spent budget."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("unreachable", request=request)

    result = _client(handler).infer(_req(deadline_ms=30_000), {})
    assert result.outcome == "transport"
    assert result.status == 502


def test_our_timeout_without_a_budget_is_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _client(handler).infer(_req(), {}).outcome == "transport"


def test_a_non_http_exception_is_unexpected_never_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug")

    assert _client(handler).infer(_req(), {}).outcome == "unexpected"


# ── Retry-After ───────────────────────────────────────────────────────────────


def test_retry_after_seconds_is_kept_on_503() -> None:
    body = {"error": "overloaded"}
    result = _client(_answer(503, body, {"Retry-After": "7"})).infer(_req(), {})
    assert result.outcome == "overloaded"
    assert result.retry_after == 7.0


def test_retry_after_http_date_is_seconds_from_now() -> None:
    now = 1_700_000_000.0
    header = email.utils.formatdate(now + 42, usegmt=True)
    result = classify_response(
        503, {"error": "overloaded"}, {"Retry-After": header}, now=lambda: now
    )
    assert result.retry_after == pytest.approx(42.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("garbage", None),
        ("soon", None),
        ("1e308", None),  # float grammar, not 1*DIGIT
        ("1_0", None),  # Python int grammar, not 1*DIGIT
        ("-5", None),
        ("+5", None),
        ("2.5", None),
        ("nan", None),
        ("0x10", None),
        ("٣", None),  # a non-ASCII digit
        ("Sunday, 06-Nov-94 08:49:37 GMT", None),  # obsolete RFC 850 form
        ("Sun Nov  6 08:49:37 1994", None),  # obsolete asctime form
        ("Sun, 32 Nov 1994 08:49:37 GMT", None),  # an impossible date
        ("Sun, 06 Nov 1994 08:49:37 +0000", None),  # IMF-fixdate is GMT only
        ("0", 0.0),
        ("7", 7.0),
        (" 7 ", 7.0),
        ("300", 300.0),
        ("301", RETRY_AFTER_MAX_S),
        ("99999999999", RETRY_AFTER_MAX_S),
        ("9" * 100_000, RETRY_AFTER_MAX_S),
    ],
)
def test_parse_retry_after_accepts_only_rfc9110_forms(
    value: str | None, expected: float | None
) -> None:
    assert parse_retry_after(value) == expected


def test_a_far_future_retry_after_date_is_clamped() -> None:
    assert parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT") == RETRY_AFTER_MAX_S
    assert RETRY_AFTER_MAX_S == 300.0


def test_a_valid_imf_fixdate_is_seconds_from_now() -> None:
    now = 784_111_777.0 - 60  # one minute before Sun, 06 Nov 1994 08:49:37 GMT
    assert parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", now=lambda: now) == 60.0


def test_a_retry_after_date_in_the_past_is_zero() -> None:
    now = 1_700_000_000.0
    header = email.utils.formatdate(now - 100, usegmt=True)
    assert parse_retry_after(header, now=lambda: now) == 0.0


def test_a_503_without_retry_after_has_none() -> None:
    assert _client(_answer(503, {"error": "overloaded"})).infer(_req(), {}).retry_after is None


# ── the request: headers, body, timeout ──────────────────────────────────────


def test_the_budget_and_traceparent_headers_are_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"prediction": 1.0})

    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    _client(handler).infer(_req(deadline_ms=250, traceparent=tp), {"x": 1})
    assert seen[0].headers[BUDGET_HEADER] == "250"
    assert seen[0].headers["traceparent"] == tp
    assert seen[0].url.path == INFER_PATH


def test_no_budget_means_no_budget_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"prediction": 1.0})

    _client(handler).infer(_req(), {})
    assert BUDGET_HEADER not in seen[0].headers
    assert "traceparent" not in seen[0].headers


def test_the_timeout_is_derived_from_the_budget() -> None:
    timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"prediction": 1.0})

    client = _client(handler)
    client.infer(_req(deadline_ms=250), {})
    client.infer(_req(), {})
    assert timeouts[0]["read"] == pytest.approx(1.25)  # budget + 1 s grace
    assert timeouts[1]["read"] == pytest.approx(10.0)  # the platform default


def test_routing_fields_win_over_the_payload() -> None:
    import json

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"prediction": 1.0})

    req = _req(metadata={"job_id": 17})
    _client(handler).infer(req, {"model_name": "evil", "embedding": [1.0], "num_nodes": 2})
    assert bodies[0]["model_name"] == "JPCP"
    assert bodies[0]["alias"] == "Production"
    assert bodies[0]["job_id"] == "17"
    assert bodies[0]["num_nodes"] == 2


def test_effective_budget_is_the_min_ignoring_nones() -> None:
    assert effective_budget_ms(500, 200) == 200
    assert effective_budget_ms(200, 500) == 200
    assert effective_budget_ms(None, 300) == 300
    assert effective_budget_ms(300, None) == 300
    assert effective_budget_ms(None, None) is None


def test_the_base_url_comes_from_the_env_then_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INFERENCE_URL_ENV, raising=False)
    assert RayPipelineClient().base_url == DEFAULT_INFERENCE_URL
    monkeypatch.setenv(INFERENCE_URL_ENV, "http://elsewhere:9000/")
    assert RayPipelineClient().base_url == "http://elsewhere:9000"
    assert RayPipelineClient("http://explicit:1").base_url == "http://explicit:1"


def test_one_shared_http_client_serves_every_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """I1: one pool for the process, however many threads call in (anyio churns its workers)."""
    real = httpx.Client
    created: list[httpx.Client] = []

    class _Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(httpx, "Client", _Counting)
    callers: set[int] = set()
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            callers.add(threading.get_ident())
        return httpx.Response(200, json={"prediction": 1.0})

    client = _client(handler)
    barrier = threading.Barrier(16)

    def work() -> None:
        barrier.wait(timeout=10)
        assert client.infer(_req(), {}).outcome == "ok"

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(callers) == 16
    assert len(created) == 1
    client.close()
    assert created[0].is_closed
    assert client.closed


def test_close_is_idempotent_and_a_closed_client_answers_transport() -> None:
    client = _client(_answer(200, {"prediction": 1.0}))
    client.close()
    client.close()
    result = client.infer(_req(), {})
    assert result.outcome == "transport"
    assert result.status == 502


def test_the_pool_is_bounded_and_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MAX_CONNECTIONS_ENV, raising=False)
    assert RayPipelineClient("http://x").limits.max_connections == 100
    monkeypatch.setenv(MAX_CONNECTIONS_ENV, "7")
    assert RayPipelineClient("http://x").limits.max_connections == 7
    monkeypatch.setenv(MAX_CONNECTIONS_ENV, "lots")
    assert RayPipelineClient("http://x").limits.max_connections == 100
    assert RayPipelineClient("http://x", max_connections=3).limits.max_connections == 3


def test_the_request_body_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    caplog.set_level(logging.DEBUG)
    result = _client(handler).infer(_req(), {"secret_field": "hunter2-payload"})
    assert result.outcome == "transport"
    assert "hunter2-payload" not in caplog.text


def test_the_shared_client_never_stores_or_replays_an_upstream_cookie() -> None:
    """P2: one process-wide client serves every tenant, so an upstream ``Set-Cookie`` must never
    be kept and sent back on somebody else's request."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            json={"prediction": 1.0},
            headers={"set-cookie": "session=tenant-a-secret; Path=/"},
        )

    client = _client(handler)
    client.infer(_req(), {})
    client.infer(_req(), {})
    assert seen == [None, None]  # nothing sent back on the second request
    assert len(client._http.cookies.jar) == 0  # and nothing stored at all
