# tests/unit/test_gateway_reasoning_budget.py
"""ADR 0035 clause 2 - the gateway enforces a reasoning budget and accounts thinking separately.

The recorded gap was that the budget was "never consulted by the gateway" and that reasoning
tokens appeared "in neither the C1 GenAI telemetry nor FinOps". These drive the real
``GatewayClient`` against a real HTTP server that reports OpenAI-shaped usage
(``completion_tokens_details.reasoning_tokens``), plus dict backends for the shapes a real server
does not produce (no usage at all).
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops.data import reasoning_budgets as store  # noqa: E402
from examlops.data.events import reasoning_usage_summary  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402
from examlops.structured import get_reasoning_trace  # noqa: E402

MSGS = [{"role": "user", "content": "think hard"}]


class _Server:
    def __init__(self):
        self.reasoning_tokens: int | None = 40
        self.completion_tokens = 60
        self.reasoning_text: str | None = None
        self.requests: list[dict] = []


@pytest.fixture
def server(monkeypatch):
    state = _Server()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            usage: dict = {"prompt_tokens": 5, "completion_tokens": state.completion_tokens}
            if state.reasoning_tokens is not None:
                usage["completion_tokens_details"] = {"reasoning_tokens": state.reasoning_tokens}
            message: dict = {"content": "the answer"}
            if state.reasoning_text is not None:
                message["reasoning_content"] = state.reasoning_text
            data = json.dumps(
                {"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")
    monkeypatch.delenv("EXAMLOPS_REASONING_BUDGET_MODE", raising=False)
    monkeypatch.delenv("EXAMLOPS_REASONING_BUDGET_DEFAULT", raising=False)
    monkeypatch.delenv("EXAMLOPS_REASONING_TRACE_CAPTURE", raising=False)
    init_db()
    yield state
    httpd.shutdown()


def _client(server, *, cap_param: str | None = "thinking_token_budget", **client_kw):
    block = {"engine": "vllm-server", "mode": "server", "base_url": server.base_url}
    if cap_param:
        block["reasoning_cap_param"] = cap_param
    router = gw.Router()
    router.add_route("m", [("server", gw.engine_backend("test-model", block))])
    return gw.GatewayClient(router, **client_kw)


def _events(**kw):
    return store.list_events(**kw)


def _audit_actions():
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM audit_events")]


# -- under budget ---------------------------------------------------------------------------


def test_under_budget_is_served_accounted_and_recorded(server):
    store.put("model", "m", 100)

    comp = _client(server).chat("m", MSGS)

    assert comp.text == "the answer"
    assert comp.reasoning_tokens == 40
    assert comp.reasoning_status == "within"
    summary = reasoning_usage_summary("m")
    # 60 completion tokens, 40 of them thinking: the split is reasoning 40 + output 20.
    assert (summary["reasoning_tokens"], summary["output_tokens"]) == (40, 20)
    (event,) = _events()
    assert (event["outcome"], event["budget_tokens"], event["observed_tokens"]) == (
        "within",
        100,
        40,
    )
    assert "reasoning_budget_exceeded" not in _audit_actions()


def test_the_cap_reaches_a_server_that_declared_a_field_for_it(server):
    store.put("model", "m", 100)

    _client(server).chat("m", MSGS)

    assert server.requests[0]["thinking_token_budget"] == 100


def test_no_cap_is_invented_for_a_server_that_declared_none(server):
    """Unset ``reasoning_cap_param`` means nothing goes on the wire - never a guessed name."""
    store.put("model", "m", 100)

    comp = _client(server, cap_param=None).chat("m", MSGS)

    body = server.requests[0]
    assert set(body) <= {"model", "messages"}, body
    assert comp.reasoning_status == "within", "still enforced afterwards, from reported usage"


# -- over budget ----------------------------------------------------------------------------


def test_over_budget_is_refused_after_being_accounted_and_audited(server):
    store.put("model", "m", 10)  # the server reports 40

    with pytest.raises(gw.ReasoningBudgetExceeded) as err:
        _client(server).chat("m", MSGS)

    assert (err.value.observed, err.value.budget, err.value.source) == (40, 10, "model")
    # The tokens were spent: they stay in the ledger even though the answer is withheld.
    assert reasoning_usage_summary("m")["reasoning_tokens"] == 40
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gateway_calls").fetchone()[0] == 1
    (event,) = _events()
    assert event["outcome"] == "refused"
    assert "reasoning_budget_exceeded" in _audit_actions()


def test_flag_mode_serves_the_response_but_still_records_and_audits(server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REASONING_BUDGET_MODE", "flag")
    store.put("model", "m", 10)

    comp = _client(server).chat("m", MSGS)

    assert comp.text == "the answer" and comp.reasoning_status == "exceeded"
    assert _events()[0]["outcome"] == "exceeded"
    assert "reasoning_budget_exceeded" in _audit_actions()


def test_a_refused_response_is_never_cached(server):
    store.put("model", "m", 10)
    stored: list = []
    client = _client(server, cache_store=lambda *a, **k: stored.append(a))

    with pytest.raises(gw.ReasoningBudgetExceeded):
        client.chat("m", MSGS)

    assert stored == []


def test_the_tightest_applicable_budget_wins_and_is_named(server):
    store.put("model", "m", 500)
    key = gw.issue_virtual_key("default", "proj-a", None, None, "tester")
    from examlops.gateway import _hash_key

    store.put("project", "proj-a", 20)
    store.put("key", _hash_key(key), 300)  # looser than the project: cannot loosen it

    with pytest.raises(gw.ReasoningBudgetExceeded) as err:
        _client(server, virtual_key=key).chat("m", MSGS)

    assert (err.value.budget, err.value.source) == (20, "project")


def test_a_request_can_tighten_but_not_loosen(server):
    store.put("model", "m", 30)  # observed 40 > 30

    with pytest.raises(gw.ReasoningBudgetExceeded):
        _client(server).chat("m", MSGS, reasoning_budget=1000)  # cannot lift the model cap

    store.put("model", "m", 1000)
    with pytest.raises(gw.ReasoningBudgetExceeded) as err:
        _client(server).chat("m", MSGS, reasoning_budget=5)
    assert err.value.source == "request"


def test_the_env_default_applies_when_nothing_else_does(server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REASONING_BUDGET_DEFAULT", "8")

    with pytest.raises(gw.ReasoningBudgetExceeded) as err:
        _client(server).chat("m", MSGS)

    assert err.value.source == "default"


# -- no budget: behaviour unchanged ---------------------------------------------------------


def test_with_no_budget_nothing_is_refused_recorded_or_capped(server):
    comp = _client(server).chat("m", MSGS)

    assert comp.text == "the answer" and comp.reasoning_status is None
    assert _events() == []
    assert "thinking_token_budget" not in server.requests[0]
    assert "reasoning_budget_exceeded" not in _audit_actions()
    # Accounting is independent of budgets: a reported count is always split out.
    assert reasoning_usage_summary("m")["reasoning_tokens"] == 40


def test_a_backend_that_knows_nothing_of_reasoning_is_untouched():
    router = gw.Router()
    router.add_route("m", [("p", lambda model, messages, **kw: gw.Completion("hi", model, ""))])
    init_db()

    comp = gw.GatewayClient(router).chat("m", MSGS)

    assert comp.text == "hi" and comp.reasoning_tokens is None


# -- unknown is not zero --------------------------------------------------------------------


def test_missing_reasoning_usage_is_unknown_never_a_pass(server):
    server.reasoning_tokens = None  # the server reports no completion_tokens_details
    store.put("model", "m", 10)

    comp = _client(server).chat("m", MSGS)

    assert comp.reasoning_tokens is None
    assert comp.reasoning_status == "unknown"
    assert _events()[0]["outcome"] == "unknown"
    assert _events()[0]["observed_tokens"] is None
    assert reasoning_usage_summary("m")["reasoning_tokens"] == 0, "nothing was invented"


def test_strict_mode_refuses_unknown(server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REASONING_BUDGET_MODE", "strict")
    server.reasoning_tokens = None
    store.put("model", "m", 10)

    with pytest.raises(gw.ReasoningBudgetExceeded):
        _client(server).chat("m", MSGS)

    assert _events()[0]["outcome"] == "refused"


def test_a_dict_backend_reports_usage_in_the_openai_shape():
    router = gw.Router()
    router.add_route(
        "m",
        [
            (
                "d",
                lambda model, messages, **kw: {
                    "text": "x",
                    "completion_tokens": 30,
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 25}},
                },
            )
        ],
    )
    init_db()
    store.put("model", "m", 10)

    with pytest.raises(gw.ReasoningBudgetExceeded) as err:
        gw.GatewayClient(router).chat("m", MSGS)

    assert err.value.observed == 25


def test_a_negative_or_boolean_count_is_unknown_not_accepted():
    from examlops.structured import reasoning_tokens_from_usage as f

    assert f({"completion_tokens_details": {"reasoning_tokens": -1}}) is None
    assert f({"completion_tokens_details": {"reasoning_tokens": True}}) is None
    assert f({"completion_tokens_details": {}}) is None
    assert f(None) is None
    assert f({"completion_tokens_details": {"reasoning_tokens": 0}}) == 0


# -- traces are content: redacted through the ADR 0148 d2 redactor --------------------------


def test_a_trace_is_not_captured_unless_asked(server):
    server.reasoning_text = "thinking about alice@example.com"

    _client(server).chat("m", MSGS)

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reasoning_traces").fetchone()[0] == 0


def test_a_captured_trace_is_redacted_and_never_returned_to_the_caller(server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REASONING_TRACE_CAPTURE", "1")
    server.reasoning_text = "thinking about alice@example.com"

    comp = _client(server).chat("m", MSGS)

    assert comp.reasoning_text is None, "raw reasoning never rides back unscanned"
    with get_db() as conn:
        (rid, stored) = conn.execute(
            "SELECT request_id, redacted_trace FROM reasoning_traces"
        ).fetchone()
    assert "alice@example.com" not in stored
    assert get_reasoning_trace(rid) == stored


def test_the_trace_uses_the_telemetry_redactor_and_fails_closed(server, monkeypatch):
    import examlops.guardrails as guardrails

    monkeypatch.setenv("EXAMLOPS_REASONING_TRACE_CAPTURE", "1")
    server.reasoning_text = "raw trace"
    used: list[str] = []

    def boom(tenant="default", mode=None):
        used.append(tenant)
        raise RuntimeError("redactor down")

    monkeypatch.setattr(guardrails, "telemetry_redactor", boom)

    comp = _client(server).chat("m", MSGS)

    assert used, "ADR 0148 d2's telemetry redactor is the one consulted"
    assert comp.text == "the answer", "a redaction failure does not fail the request"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reasoning_traces").fetchone()[0] == 0


# -- C1 telemetry ---------------------------------------------------------------------------


def test_the_span_carries_reasoning_tokens_only_when_reported():
    from examlops.telemetry import genai

    class Span:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v

    known, unknown = Span(), Span()
    genai.record_usage(known, model="m", output_tokens=60, reasoning_tokens=40)
    genai.record_usage(unknown, model="m", output_tokens=60)

    assert known.attrs["examlops.usage.reasoning_tokens"] == 40
    assert "examlops.usage.reasoning_tokens" not in unknown.attrs


# -- configuration surface ------------------------------------------------------------------


def test_engine_block_validates_the_cap_param_name():
    from examlops.engines.config import validate_engine_block

    assert (
        validate_engine_block({"engine": "vllm", "reasoning_cap_param": "thinking_token_budget"})
        == []
    )
    assert validate_engine_block({"engine": "vllm", "reasoning_cap_param": "not a name"})


def test_cli_sets_lists_and_removes_budgets():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    out = runner.invoke(app, ["--json", "gateway", "reasoning", "set-budget", "50", "--model", "m"])
    assert out.exit_code == 0, out.output
    assert store.list_budgets()[0]["max_thinking_tokens"] == 50
    out = runner.invoke(app, ["--json", "gateway", "reasoning", "budgets"])
    assert json.loads(out.output)[0]["ref"] == "m"
    bad = runner.invoke(app, ["gateway", "reasoning", "set-budget", "5"])
    assert bad.exit_code != 0 and len(store.list_budgets()) == 1
    out = runner.invoke(
        app, ["--json", "gateway", "reasoning", "set-budget", "0", "--model", "m", "--remove"]
    )
    assert store.list_budgets() == [] and out.exit_code == 0


def test_the_gateway_has_no_streaming_path_to_bypass_the_gate():
    """Recorded, not assumed: ``GatewayClient`` exposes no ``stream``/``chat_stream``, so every
    completion passes the gate above. The day one is added it must route through
    ``_reasoning_gate`` - this test failing is the reminder."""
    assert not hasattr(gw.GatewayClient, "stream") and not hasattr(gw.GatewayClient, "chat_stream")
