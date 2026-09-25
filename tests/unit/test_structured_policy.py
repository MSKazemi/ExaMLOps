# tests/unit/test_structured_policy.py
"""ADR 0035 clause 3 — default schemas, per-route defaults, and budgets gated via D5.

The recorded gap was that clause 3 "has no configuration surface at all". These drive the real
``GatewayClient`` (dict backends, so the outcome is the returned/refused completion, never argv),
the real ``RagPipeline.query`` and the real ``exa gateway`` commands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops.data import reasoning_budgets as store  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402
from examlops.structured import StructuredOutputError, resolve_reasoning_budget  # noqa: E402
from examlops.structured import policy as sp  # noqa: E402

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cfg = tmp_path / "structured.yaml"
    monkeypatch.setenv(sp.ENV_PATH, str(cfg))
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")
    for var in (
        "EXAMLOPS_REASONING_BUDGET_MODE",
        "EXAMLOPS_REASONING_BUDGET_DEFAULT",
        "EXAMLOPS_REASONING_TRACE_CAPTURE",
    ):
        monkeypatch.delenv(var, raising=False)
    sp._cache = None
    init_db()
    yield cfg
    sp._cache = None


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    sp._cache = None


def _client(text: str, *, route: str = "m", reasoning: int | None = None, calls=None):
    def backend(model, messages, **kw):
        if calls is not None:
            calls.append(kw)
        out = {"text": text, "completion_tokens": 50}
        if reasoning is not None:
            out["reasoning_tokens"] = reasoning
        return out

    router = gw.Router()
    router.add_route(route, [("b", backend)])
    return gw.GatewayClient(router)


# ── built-in registry ──────────────────────────────────────────────────────────


def test_builtin_schemas_are_valid_json_schemas():
    for name, schema in sp.BUILTIN_SCHEMAS.items():
        assert sp._check_schema(schema) == [], name


def test_get_schema_returns_a_copy_callers_cannot_corrupt():
    s = sp.get_schema("tool_call")
    s["required"].append("evil")
    assert "evil" not in sp.get_schema("tool_call")["required"]


def test_unknown_schema_names_the_known_ones():
    with pytest.raises(sp.UnknownSchemaError) as exc:
        sp.get_schema("nope")
    assert "rag_answer" in str(exc.value)


# ── structured.yaml validation (total, fail-whole) ───────────────────────────


def test_validate_reports_every_problem_with_its_path():
    errors = sp.validate_config(
        {
            "bogus": 1,
            "schemas": {"tool_call": {"type": "object"}, "bad": {"type": 5}},
            "routes": {
                "chat": {"response_schema": "missing", "reasoning_budget": -1},
                "x": {"colour": "red"},
            },
        }
    )
    joined = "\n".join(errors)
    assert "bogus: unknown top-level key" in joined
    assert "schemas.tool_call: redefines a built-in" in joined
    assert "schemas.bad:" in joined
    assert "routes.chat.response_schema: unknown schema 'missing'" in joined
    assert "routes.chat.reasoning_budget" in joined
    assert "routes.x.colour: unknown key" in joined


def test_boolean_is_not_a_budget():
    assert sp.validate_config({"routes": {"a": {"reasoning_budget": True}}})


def test_invalid_file_is_ignored_whole_at_request_time(_isolated):
    _write(
        _isolated,
        "routes:\n  m: {reasoning_budget: 100}\n  n: {response_schema: missing}\n",
    )
    with pytest.raises(sp.StructuredConfigError):
        sp.load_config()
    # the valid half is NOT applied: which defaults are in force never depends on typo position
    assert sp.route_defaults("m").reasoning_budget is None


def test_site_schema_and_glob_route_defaults(_isolated):
    _write(
        _isolated,
        """
schemas:
  ticket:
    type: object
    properties: {severity: {type: integer}}
    required: [severity]
routes:
  "support-*": {response_schema: ticket, reasoning_budget: 900}
  support-eu: {reasoning_budget: 300}
""",
    )
    d = sp.route_defaults("support-eu")
    assert d.response_schema == "ticket"
    assert d.reasoning_budget == 300  # tightest of every matching entry
    assert sp.route_defaults("support-us").reasoning_budget == 900
    assert sp.route_defaults("other") == sp.RouteDefaults()
    assert {r["name"] for r in sp.list_schemas()} >= {"ticket", "rag_answer"}


# ── per-route reasoning budget in the resolver ───────────────────────────────


def test_route_budget_is_a_candidate_and_tightest_wins(_isolated):
    _write(_isolated, "routes:\n  m: {reasoning_budget: 500}\n")
    b = resolve_reasoning_budget("m")
    assert (b.max_thinking_tokens, b.source) == (500, "route")
    store.put("model", "m", 200)
    b = resolve_reasoning_budget("m")
    assert (b.max_thinking_tokens, b.source) == (200, "model")
    # a route default can tighten but never lift a caller's own cap
    assert resolve_reasoning_budget("m", requested=50).max_thinking_tokens == 50


def test_route_budget_is_enforced_by_the_gateway(_isolated):
    _write(_isolated, "routes:\n  m: {reasoning_budget: 10}\n")
    with pytest.raises(gw.ReasoningBudgetExceeded) as exc:
        _client("ok", reasoning=40).chat("m", MSGS)
    assert exc.value.source == "route"
    events = store.list_events(outcome="refused")
    assert events and events[0]["source"] == "route"


# ── named + route-default schemas through the gateway ────────────────────────


def test_chat_accepts_a_schema_name():
    comp = _client('{"name": "search", "arguments": {"q": "x"}, "junk": 1}').chat(
        "m", MSGS, response_schema="tool_call"
    )
    assert comp.parsed == {"name": "search", "arguments": {"q": "x"}}  # extras repaired away


def test_unknown_schema_name_fails_before_any_backend_call():
    calls: list = []
    with pytest.raises(sp.UnknownSchemaError):
        _client("{}", calls=calls).chat("m", MSGS, response_schema="nope")
    assert calls == []


def test_route_default_schema_applies_when_the_caller_names_none(_isolated):
    _write(_isolated, "routes:\n  m: {response_schema: classification}\n")
    comp = _client('{"label": "spam", "confidence": "0.9"}').chat("m", MSGS)
    assert comp.parsed == {"label": "spam", "confidence": 0.9}
    with pytest.raises(StructuredOutputError):
        _client("not json at all").chat("m", MSGS)
    # a caller's own schema still wins over the route default
    own = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    assert _client('{"x": 3}').chat("m", MSGS, response_schema=own).parsed == {"x": 3}


def test_no_config_leaves_plain_text_requests_untouched():
    comp = _client("plain words").chat("m", MSGS)
    assert comp.text == "plain words" and comp.parsed is None


# ── D5 gates ─────────────────────────────────────────────────────────────────


def _policy(tmp_path: Path, text: str) -> None:
    (tmp_path / "policy.yaml").write_text(text)


def _audit_actions() -> list[str]:
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT action FROM audit_events").fetchall()]


def test_d5_denies_an_unbudgeted_request_before_any_backend(tmp_path):
    pytest.importorskip("simpleeval")
    _policy(
        tmp_path,
        "policies:\n  - action: reasoning_request\n    when: 'not has_budget'\n    effect: deny\n",
    )
    calls: list = []
    with pytest.raises(gw.ReasoningPolicyDenied) as exc:
        _client("ok", calls=calls).chat("m", MSGS)
    assert exc.value.effect == "deny"
    assert calls == []
    assert "policy:reasoning_request" in _audit_actions()
    # the same request with a budget passes the rule
    comp = _client("ok", reasoning=5).chat("m", MSGS, reasoning_budget=100)
    assert comp.text == "ok"


def test_d5_ceiling_on_budget_size(tmp_path):
    pytest.importorskip("simpleeval")
    _policy(
        tmp_path,
        "policies:\n  - action: reasoning_request\n    when: 'budget_tokens > 4000'\n"
        "    effect: require_approval\n",
    )
    with pytest.raises(gw.ReasoningPolicyDenied) as exc:
        _client("ok").chat("m", MSGS, reasoning_budget=8000)
    assert exc.value.effect == "require_approval"  # no human inside a request: a refusal
    assert _client("ok", reasoning=1).chat("m", MSGS, reasoning_budget=1000).text == "ok"


def test_no_policy_writes_no_per_request_audit_rows():
    before = len(_audit_actions())
    _client("ok").chat("m", MSGS)
    assert [a for a in _audit_actions()[before:] if a.startswith("policy")] == []


def test_a_raising_policy_engine_denies(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr("examlops.policy.decide", boom)
    with pytest.raises(gw.ReasoningPolicyDenied):
        _client("ok").chat("m", MSGS)


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli():
    from examlops.cli.commands.gateway_cmd import app

    runner = CliRunner()
    return lambda *args: runner.invoke(app, list(args))


def test_cli_schema_list_and_show(cli, _isolated):
    _write(_isolated, "routes:\n  m: {response_schema: rag_answer, reasoning_budget: 7}\n")
    res = cli("schema", "list")
    assert res.exit_code == 0, res.output
    assert "rag_answer" in res.output and "tool_call" in res.output
    res = cli("schema", "show", "rag_answer")
    assert res.exit_code == 0 and '"citations"' in res.output
    res = cli("schema", "show", "nope")
    assert res.exit_code == 2


def test_cli_schema_list_fails_on_invalid_config(cli, _isolated):
    _write(_isolated, "routes:\n  m: {response_schema: missing}\n")
    res = cli("schema", "list")
    assert res.exit_code == 1
    assert "routes.m.response_schema" in res.output


def test_cli_schema_test_accepts_a_registered_name(cli, tmp_path):
    obj = tmp_path / "o.json"
    obj.write_text(json.dumps({"label": "a"}))
    assert cli("schema", "test", "classification", str(obj)).exit_code == 0
    obj.write_text(json.dumps({"label": ""}))
    assert cli("schema", "test", "classification", str(obj), "--no-repair").exit_code == 1
    assert cli("schema", "test", "no-such-schema", str(obj)).exit_code == 2


def test_cli_set_budget_is_audited_and_policy_gated(cli, tmp_path):
    res = cli("reasoning", "set-budget", "300", "--model", "m")
    assert res.exit_code == 0, res.output
    assert store.list_budgets()[0]["max_thinking_tokens"] == 300
    assert "reasoning_budget_set" in _audit_actions()

    _policy(tmp_path, "policies:\n  - action: reasoning_budget_set\n    effect: deny\n")
    res = cli("reasoning", "set-budget", "9000", "--model", "m")
    assert res.exit_code == 1
    assert "Refused by policy" in res.output
    assert store.list_budgets()[0]["max_thinking_tokens"] == 300  # unchanged
    res = cli("reasoning", "set-budget", "0", "--model", "m", "--remove")
    assert res.exit_code == 1 and store.list_budgets()  # removal is gated too


# ── review fixes ─────────────────────────────────────────────────────────────


def test_cli_chat_under_a_route_default_schema_refuses_cleanly(cli, _isolated):
    """A route default schema makes `exa gateway chat` structured; an unusable answer used to
    escape as a StructuredOutputError traceback (it is not a GatewayError)."""
    _write(_isolated, "routes:\n  default: {response_schema: rag_answer}\n")
    res = cli("chat", "default", "--message", "plain words, no object")
    assert res.exit_code == 1, res.output
    assert isinstance(res.exception, SystemExit)  # a handled refusal, not an uncaught error
    assert "StructuredOutputError" in res.output


def test_cli_chat_json_carries_the_parsed_object(_isolated):
    from examlops.cli import _output
    from examlops.cli.commands.gateway_cmd import app

    _write(_isolated, "routes:\n  default: {response_schema: rag_answer}\n")
    msg = json.dumps({"answer": "a", "citations": [1]})
    prev = (_output.json_mode, _output.output_format)
    _output.json_mode, _output.output_format = True, "json"
    try:
        res = CliRunner().invoke(app, ["chat", "default", "--message", msg])
    finally:
        _output.json_mode, _output.output_format = prev
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["parsed"] == {"answer": "a", "citations": [1]}


def test_budget_removal_is_not_judged_on_the_ignored_positional_value(cli, tmp_path):
    pytest.importorskip("simpleeval")
    assert cli("reasoning", "set-budget", "300", "--model", "m").exit_code == 0
    _policy(
        tmp_path,
        "policies:\n  - action: reasoning_budget_set\n    when: 'max_thinking_tokens > 4000'\n"
        "    effect: deny\n",
    )
    # `--remove` still takes the positional; its value sets nothing and must not trip a ceiling.
    res = cli("reasoning", "set-budget", "9000", "--model", "m", "--remove")
    assert res.exit_code == 0, res.output
    assert store.list_budgets() == []
