"""The agent tool broker (ADR 0145, tool half).

Real code paths: the real registry functions from ``examlops.mcp.tools``, a real sqlite
``platform.db`` (the suite's per-test one), the real audit chain, the real plan/apply machinery and
the real CLI. Fake tools appear only where the registry has no tool with the property under test
(a URL argument, a credential parameter); they are real ``ToolSpec`` objects run by the real broker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import plans  # noqa: E402
from examlops import tool_broker as tb  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import tool_grants as store  # noqa: E402
from examlops.data.audit import (  # noqa: E402
    dropped_audit_events,
    export_audit_events,
    reset_dropped_audit_events,  # noqa: E402
)
from examlops.mcp import server as mcp_server  # noqa: E402
from examlops.mcp import tools  # noqa: E402
from examlops.mcp.tools import ToolSpec  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.tool_broker.grants import (  # noqa: E402
    GrantSet,
    ToolCall,
    check_schema,
    decide_tool_call,
    parse_grant,
    validate_args,
)

runner = CliRunner()
SPLIT = {"model": "JPCP", "production": 90, "canary": 10}
AGENT = tb.ToolCaller(agent="jobdoc", version_id="av-sha256:" + "b" * 64, session="s1")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    for k in (
        "EXAMLOPS_PRINCIPAL_KIND",
        "EXAMLOPS_TOOL_BROKER",
        "EXAMLOPS_AGENT_NAME",
        "EXAMLOPS_AGENT_VERSION_ID",
        "EXAMLOPS_AGENT_SUBJECT",
    ):
        monkeypatch.delenv(k, raising=False)
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])
    init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _events(prefix="tool_broker:"):
    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


def _details(e):
    d = e["details"]
    return json.loads(d) if isinstance(d, str) else d


def _grant(tool, subject="jobdoc", **doc):
    return tb.set_grant(subject, tool, doc or {"effect": "allow"})


def _traffic():
    return tools.traffic_rules("JPCP")["rules"]


# ── the pure decision: the default, stated precisely ─────────────────────────────────────


def _set(**grants):
    return GrantSet("jobdoc", {t: parse_grant(t, d) for t, d in grants.items()})


def _call(tool="list_models", args=None, tier="read"):
    return ToolCall(AGENT, tool, args or {}, tier)


def test_no_grant_set_is_allow_as_today():
    d = decide_tool_call(None, _call())
    assert d.effect == "allow" and d.code == "no_grant_set"


def test_a_grant_set_is_default_deny_for_an_ungranted_tool():
    d = decide_tool_call(_set(other={}), _call())
    assert d.effect == "deny" and d.code == "no_grant"


def test_an_exact_grant_overrides_the_star_grant_and_deny_is_explicit():
    gs = _set(**{"*": {"effect": "deny"}, "list_models": {"effect": "allow"}})
    assert decide_tool_call(gs, _call("list_models")).effect == "allow"
    assert decide_tool_call(gs, _call("model_detail")).code == "denied_by_grant"


def test_tier_ceiling_denies_a_higher_tier_and_allows_an_equal_one():
    gs = _set(set_traffic_split={"tier_ceiling": "A"}, apply_plan={"tier_ceiling": "A"})
    assert decide_tool_call(gs, _call("set_traffic_split", tier="A")).allowed
    d = decide_tool_call(gs, _call("apply_plan", tier="B"))
    assert d.code == "tier_exceeds_ceiling"


def test_needs_approval_yields_require_approval():
    gs = _set(set_traffic_split={"needs_approval": True})
    assert decide_tool_call(gs, _call("set_traffic_split", tier="A")).effect == "require_approval"


def test_schema_subset_rejects_unsupported_keywords_and_bad_patterns():
    assert check_schema({"type": "object", "properties": {"a": {"type": "string"}}}) == []
    assert any("unsupported" in p for p in check_schema({"oneOf": []}))
    assert any("regular expression" in p for p in check_schema({"pattern": "("}))
    assert any("at most" in p for p in check_schema({"pattern": "a" * 300}))


def test_validate_args_never_echoes_the_offending_value():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["model"],
        "properties": {"model": {"type": "string", "pattern": "jpcp.*"}},
    }
    bad = validate_args(schema, {"model": "SUPERSECRETVALUE", "extra": 1})
    assert bad and not any("SUPERSECRETVALUE" in p for p in bad)
    assert any("not an allowed argument" in p for p in bad)
    assert validate_args(schema, {"model": "jpcp-2"}) == []
    assert any("required" in p for p in validate_args(schema, {}))


def test_invalid_grants_are_refused_with_every_problem_named():
    with pytest.raises(tb.GrantError) as e:
        parse_grant(
            "x", {"effect": "maybe", "tier_ceiling": "Z", "max_calls_per_minute": 0, "z": 1}
        )
    assert len(e.value.problems) >= 4


def test_set_grant_refuses_an_unknown_tool_name():
    with pytest.raises(tb.GrantError, match="not a registered tool"):
        tb.set_grant("jobdoc", "no_such_tool", {})


# ── the broker end to end ─────────────────────────────────────────────────────────────────


def test_a_caller_with_no_grants_is_unbrokered_and_the_decision_is_audited():
    out = tb.invoke(AGENT, "authz_relations", {})
    assert out["ok"] is True
    (e,) = _events()
    d = _details(e)
    assert e["action"] == "tool_broker:allow" and d["code"] == "no_grant_set"
    assert d["agent"] == "jobdoc" and d["version_id"] == AGENT.version_id


def test_with_grants_an_ungranted_tool_is_denied_audited_and_never_runs(monkeypatch):
    _grant("authz_relations")
    ran = []
    monkeypatch.setattr(tools, "recent_audit_events", lambda *a, **k: ran.append(1))
    out = tb.invoke(AGENT, "recent_audit_events", {})
    assert out["ok"] is False and out["code"] == "no_grant" and not ran
    assert _events("tool_broker:deny")
    assert tb.invoke(AGENT, "authz_relations", {})["ok"] is True


def test_the_most_specific_grant_set_wins_whole_version_before_agent():
    _grant("authz_relations", subject="jobdoc")
    _grant("recent_audit_events", subject=AGENT.version_id)
    # the version-pinned set replaces (not extends) the agent-name set
    assert tb.invoke(AGENT, "recent_audit_events", {})["ok"] is True
    assert tb.invoke(AGENT, "authz_relations", {})["code"] == "no_grant"


def test_unknown_tool_is_denied_and_audited():
    out = tb.invoke(AGENT, "no_such_tool", {})
    assert out["code"] == "unknown_tool"
    assert _events("tool_broker:deny")


def test_rate_limit_per_minute_windows_and_denied_calls_do_not_count():
    _grant("authz_relations", max_calls_per_minute=2)
    t0 = 6000.0
    ctx = lambda now: tb.BrokerContext(now=now)  # noqa: E731
    assert tb.invoke(AGENT, "authz_relations", {}, ctx(t0))["ok"]
    assert tb.invoke(AGENT, "authz_relations", {}, ctx(t0 + 1))["ok"]
    out = tb.invoke(AGENT, "authz_relations", {}, ctx(t0 + 2))
    assert out["ok"] is False and out["code"] == "rate_limited"
    assert store.counter("jobdoc", "authz_relations", "minute", int(t0 // 60)) == 2
    assert tb.invoke(AGENT, "authz_relations", {}, ctx(t0 + 61))["ok"]  # next window
    assert any(_details(e)["code"] == "rate_limited" for e in _events("tool_broker:deny"))


def test_rate_limit_per_session_and_a_session_is_required():
    _grant("authz_relations", max_calls_per_session=1)
    assert tb.invoke(AGENT, "authz_relations", {})["ok"]
    assert tb.invoke(AGENT, "authz_relations", {})["code"] == "rate_limited"
    other = tb.ToolCaller(agent="jobdoc", version_id=AGENT.version_id, session="s2")
    assert tb.invoke(other, "authz_relations", {})["ok"]  # a new session has its own budget
    nosess = tb.ToolCaller(agent="jobdoc")
    assert tb.invoke(nosess, "authz_relations", {})["code"] == "session_required"


def test_counters_are_pruned_so_the_table_stays_bounded():
    for i in range(30):
        store.consume("s", "t", [("minute", 1000 + i, 5)], now=(1000 + i) * 60.0)
    from examlops.data import get_db

    with get_db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM tool_call_counters").fetchone()["n"]
    assert n <= 3


def test_argument_pattern_refusal_names_the_rule_not_the_value():
    _grant(
        "recent_audit_events",
        arg_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"model": {"type": "string", "pattern": "jpcp.*"}},
        },
    )
    assert tb.invoke(AGENT, "recent_audit_events", {"model": "jpcp-2"})["ok"]
    out = tb.invoke(AGENT, "recent_audit_events", {"model": "TOPSECRETNAME"})
    assert out["code"] == "argument_violation" and "TOPSECRETNAME" not in json.dumps(out)
    assert tb.invoke(AGENT, "recent_audit_events", {"limit": 3})["code"] == "argument_violation"


def test_tier_ceiling_through_the_broker_blocks_a_write_tool():
    _grant("set_traffic_split", tier_ceiling="read")
    out = tb.invoke(AGENT, "set_traffic_split", SPLIT)
    assert out["code"] == "tier_exceeds_ceiling" and _traffic() is None


def test_needs_approval_blocks_until_the_runtime_supplies_it_never_the_args():
    _grant("authz_relations", needs_approval=True)
    out = tb.invoke(AGENT, "authz_relations", {})
    assert out["ok"] is False and out["code"] == "approval_required"
    # an agent cannot approve itself by asking
    forged = tb.invoke(AGENT, "authz_relations", {"approved": True})
    assert forged["code"] == "approval_required" or forged["ok"] is False
    ok = tb.invoke(AGENT, "authz_relations", {}, tb.BrokerContext(approved=True))
    assert ok["ok"] is True
    assert _details(_events("tool_broker:allow")[-1])["approved"] is True
    assert _events("tool_broker:require_approval")


def test_audit_rows_redact_secret_keys_and_credential_shaped_text():
    _grant("recent_audit_events", arg_schema={"type": "object"})
    tb.invoke(
        AGENT,
        "recent_audit_events",
        {
            "model": "https://user:hunter2pw@host/x?token=abc123tok",
            "api_token": "s3cr3tvalue",
            "nested": {"password": "pw-nested", "note": "Authorization: Bearer eyJhbGciOiJ9"},
        },
    )
    blob = json.dumps(_events("tool_broker:"))
    for leaked in ("hunter2pw", "abc123tok", "s3cr3tvalue", "pw-nested", "eyJhbGciOiJ9"):
        assert leaked not in blob
    assert "***" in blob


# ── the credential-injection seam ─────────────────────────────────────────────────────────


def _fake(fn, tier="read"):
    return {fn.__name__: ToolSpec(fn, mutating=tier != "read", tier=tier)}


def fetch(url: str = "", api_key: str = "") -> dict:
    """A tool that reaches a network target and (badly) echoes its credential."""
    return {"ok": True, "url": url, "echo": f"key={api_key}", "have_key": bool(api_key)}


def _cred_ctx(**kw):
    return tb.BrokerContext(tools=_fake(fetch), **kw)


def test_the_credential_comes_from_the_secret_store_and_never_from_the_agent(monkeypatch):
    monkeypatch.setenv("SVC_API_KEY", "REALCRED-9f8e7d")
    from examlops.tool_broker import service

    monkeypatch.setattr(service, "_registry_names", lambda: {"fetch"})
    tb.set_grant(
        "jobdoc", "fetch", {"credentials": {"api_key": "svc/api-key"}}, known_tools={"fetch"}
    )
    out = tb.invoke(AGENT, "fetch", {"url": "https://a.example"}, _cred_ctx())
    assert out["ok"] and out["have_key"] is True
    # the tool echoed the credential; the broker scrubbed it from what the agent sees
    assert "REALCRED" not in json.dumps(out) and "***" in out["echo"]
    # ... and it is in no audit row
    assert "REALCRED" not in json.dumps(export_audit_events())


def test_an_agent_cannot_smuggle_a_credential_through_its_arguments(monkeypatch):
    monkeypatch.setenv("SVC_API_KEY", "REALCRED-9f8e7d")
    tb.set_grant(
        "jobdoc", "fetch", {"credentials": {"api_key": "svc/api-key"}}, known_tools={"fetch"}
    )
    out = tb.invoke(AGENT, "fetch", {"api_key": "AGENTSUPPLIED-123"}, _cred_ctx())
    assert out["code"] == "credential_in_args"
    blob = json.dumps(export_audit_events())
    assert "AGENTSUPPLIED-123" not in blob  # the value is masked by key name in the audit row


def test_a_missing_secret_or_a_bad_binding_denies_without_leaking(monkeypatch):
    monkeypatch.delenv("SVC_API_KEY", raising=False)
    tb.set_grant(
        "jobdoc", "fetch", {"credentials": {"api_key": "svc/api-key"}}, known_tools={"fetch"}
    )
    out = tb.invoke(AGENT, "fetch", {}, _cred_ctx())
    assert out["code"] == "credential_unavailable"
    tb.set_grant("jobdoc", "fetch", {"credentials": {"nope": "svc/api-key"}}, known_tools={"fetch"})
    assert tb.invoke(AGENT, "fetch", {}, _cred_ctx())["code"] == "credential_param_unknown"


# ── egress ────────────────────────────────────────────────────────────────────────────────


def _resolver(ip):
    return lambda host, port, *a, **k: [(2, 1, 6, "", (ip, port))]


def _egress_grant():
    tb.set_grant(
        "jobdoc",
        "fetch",
        {"egress": {"url_args": ["url"], "allowed_hosts": ["*.example.org", "api.example.com"]}},
        known_tools={"fetch"},
    )


def test_egress_refuses_a_host_outside_the_grant_allow_list():
    _egress_grant()
    out = tb.invoke(AGENT, "fetch", {"url": "https://evil.test/x"}, _cred_ctx())
    assert out["code"] == "egress_denied"
    assert tb.invoke(AGENT, "fetch", {"url": "not a url"}, _cred_ctx())["code"] == "egress_denied"


def test_egress_allows_a_listed_public_host_and_refuses_one_that_resolves_internally():
    _egress_grant()
    ok = tb.invoke(
        AGENT,
        "fetch",
        {"url": "https://a.example.org/x"},
        _cred_ctx(resolver=_resolver("93.184.216.34")),
    )
    assert ok["ok"] is True
    bad = tb.invoke(
        AGENT,
        "fetch",
        {"url": "https://a.example.org/x"},
        _cred_ctx(resolver=_resolver("10.0.0.5")),
    )
    assert bad["code"] == "egress_denied"  # allow-listed name, private address: the SSRF check


def sneaky() -> dict:
    """A tool that *claims* to be read-only (its MCP annotations say so) but is recorded tier C."""
    return {"ok": True}


def test_the_decision_rests_on_the_platform_record_not_the_tool_self_description():
    spec = ToolSpec(sneaky, mutating=False, tier="C")
    assert spec.annotations["readOnlyHint"] is True  # the hint says harmless...
    tb.set_grant("jobdoc", "sneaky", {"tier_ceiling": "read"}, known_tools={"sneaky"})
    out = tb.invoke(AGENT, "sneaky", {}, tb.BrokerContext(tools={"sneaky": spec}))
    assert out["code"] == "tier_exceeds_ceiling"  # ...the recorded tier decides


# ── monitor vs enforce ────────────────────────────────────────────────────────────────────


def test_monitor_computes_and_audits_but_never_blocks_nor_counts():
    _grant("authz_relations", max_calls_per_minute=1)
    monitor = tb.BrokerContext(mode="monitor", now=6000.0)
    for _ in range(3):
        assert tb.invoke(AGENT, "recent_audit_events", {}, monitor)["ok"]  # ungranted, still runs
    assert store.counter("jobdoc", "recent_audit_events", "minute", 100) == 0
    rows = _events("tool_broker:deny")
    assert rows and all(_details(e)["enforced"] is False for e in rows)
    # enforce, same call: blocked
    assert tb.invoke(AGENT, "recent_audit_events", {})["code"] == "no_grant"


# ── plan_required still binds an agent principal (ADR 0147 d2) ────────────────────────────


def test_plan_required_survives_the_broker_for_an_agent_principal(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    _grant("set_traffic_split", tier_ceiling="A")
    out = tb.invoke(AGENT, "set_traffic_split", SPLIT)
    assert out["ok"] is False and out["code"] == "plan_required"
    assert _traffic() is None
    assert _events("tool_broker:allow")  # the broker allowed it; the plan gate still refused


def test_a_grant_for_apply_plan_does_not_launder_a_tool_the_agent_holds_no_grant_for(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    _grant("plan_change")
    _grant("apply_plan")
    plan = tb.invoke(AGENT, "plan_change", {"tool": "set_traffic_split", "args": SPLIT})
    # planning a tool the caller may not call is refused too
    assert plan["ok"] is False and plan["code"] == "no_grant"
    # a plan made by someone else, applied through the broker by an ungranted agent
    made = tools.plan_change("set_traffic_split", SPLIT)["plan"]["plan_hash"]
    out = tb.invoke(AGENT, "apply_plan", {"plan_hash": made})
    assert out["code"] == "no_grant" and _traffic() is None


def test_plan_then_apply_through_the_broker_with_grants_and_a_human_approval(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    _grant("plan_change")
    _grant("apply_plan")
    _grant("set_traffic_split", needs_approval=True)
    made = tb.invoke(AGENT, "plan_change", {"tool": "set_traffic_split", "args": SPLIT})
    assert made["ok"] is True  # planning is free; the approval is asked at apply time
    h = made["plan"]["plan_hash"]
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND")
    token = plans.approve_plan(h)["approval_token"]
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    blocked = tb.invoke(AGENT, "apply_plan", {"plan_hash": h})
    assert blocked["code"] == "approval_required" and _traffic() is None
    done = tb.invoke(AGENT, "apply_plan", {"plan_hash": h, "approval_token": token})
    assert done["ok"] is True and _traffic()["production"] == 90


def test_idempotency_key_passes_through_the_broker():
    _grant("set_traffic_split")
    a = tb.invoke(AGENT, "set_traffic_split", {**SPLIT, "idempotency_key": "k-1"})
    b = tb.invoke(AGENT, "set_traffic_split", {**SPLIT, "idempotency_key": "k-1"})
    assert a["ok"] and b["ok"] and b.get("replayed") is True


def test_the_operator_tool_call_policy_hook_can_deny_and_ask_for_approval(monkeypatch):
    import examlops.policy as policy

    rules = [
        {
            "name": "no-audit-reads",
            "action": "tool_call",
            "when": "tool == 'recent_audit_events'",
            "effect": "deny",
        },
        {
            "name": "ask",
            "action": "tool_call",
            "when": "tool == 'authz_relations'",
            "effect": "require_approval",
        },
    ]
    monkeypatch.setattr(policy, "_load_policies", lambda path=None: rules)
    assert tb.invoke(AGENT, "recent_audit_events", {})["code"] == "policy_denied"
    assert tb.invoke(AGENT, "authz_relations", {})["code"] == "approval_required"
    assert tb.invoke(AGENT, "authz_relations", {}, tb.BrokerContext(approved=True))["ok"]


def test_an_unreadable_grant_store_fails_closed_in_enforce_open_in_monitor(monkeypatch):
    from examlops.tool_broker import broker

    def boom(_caller):
        raise RuntimeError("db down")

    monkeypatch.setattr(broker, "resolve_grant_set", boom)
    assert tb.invoke(AGENT, "authz_relations", {})["code"] == "grants_unavailable"
    assert tb.invoke(AGENT, "authz_relations", {}, tb.BrokerContext(mode="monitor"))["ok"]


# ── simulate ──────────────────────────────────────────────────────────────────────────────


def test_simulate_runs_nothing_counts_nothing_and_reads_no_secret(monkeypatch):
    monkeypatch.setenv("SVC_API_KEY", "REALCRED-9f8e7d")
    tb.set_grant(
        "jobdoc",
        "fetch",
        {"credentials": {"api_key": "svc/api-key"}, "max_calls_per_minute": 1},
        known_tools={"fetch"},
    )
    n0 = len(export_audit_events())
    for _ in range(3):
        out = tb.simulate(AGENT, "fetch", {"url": "https://a"}, _cred_ctx())
    assert out["effect"] == "allow" and out["injects_credentials"] == ["api_key"]
    assert store.counter("jobdoc", "fetch", "minute", 0) == 0
    assert len(export_audit_events()) == n0  # no audit rows, no secret_access row
    assert "REALCRED" not in json.dumps(out)


# ── the MCP server wiring ─────────────────────────────────────────────────────────────────


def _build(monkeypatch):
    seen: dict = {}

    class Fake:
        def __init__(self, name):
            pass

        def tool(self, *, name, description, annotations=None):
            def deco(fn):
                seen[name] = fn
                return fn

            return deco

        def resource(self, *a, **k):
            return lambda fn: fn

        def prompt(self, *, name, description):
            return lambda fn: fn

    monkeypatch.setattr(mcp_server, "_import_fastmcp", lambda: Fake)
    mcp_server.build_server(include_writes=True)
    return seen


def test_broker_off_registers_the_registry_functions_untouched(monkeypatch):
    seen = _build(monkeypatch)
    assert len(seen) == len(tools.REGISTRY)
    for spec in tools.REGISTRY:
        assert seen[spec.name] is spec.fn  # byte-identical: no wrapper


def test_broker_enforce_hides_ungranted_tools_and_blocks_a_forged_call(monkeypatch):
    _grant("authz_relations")
    monkeypatch.setenv("EXAMLOPS_TOOL_BROKER", "enforce")
    monkeypatch.setenv("EXAMLOPS_AGENT_NAME", "jobdoc")
    seen = _build(monkeypatch)
    assert set(seen) == {"authz_relations"}
    assert seen["authz_relations"]()["ok"] is True
    # a forged tools/call for a tool that was never listed: the wrapper path is the registry's, so
    # go through the broker directly to prove the same denial at call time
    assert tb.invoke(tb.caller_from_env(), "recent_audit_events", {})["code"] == "no_grant"


def test_broker_monitor_lists_everything_and_never_blocks(monkeypatch):
    _grant("authz_relations")
    monkeypatch.setenv("EXAMLOPS_TOOL_BROKER", "monitor")
    monkeypatch.setenv("EXAMLOPS_AGENT_NAME", "jobdoc")
    seen = _build(monkeypatch)
    assert len(seen) == len(tools.REGISTRY)
    assert seen["recent_audit_events"]()["ok"] is True
    (e,) = _events("tool_broker:deny")
    assert _details(e)["enforced"] is False


def test_an_unrecognised_broker_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_TOOL_BROKER", "enfrce")
    assert tb.broker_mode() == "enforce"
    monkeypatch.setenv("EXAMLOPS_TOOL_BROKER", "")
    assert tb.broker_mode() == "off"


# ── the CLI ───────────────────────────────────────────────────────────────────────────────


def _run(*args):
    return runner.invoke(app, list(args))


def test_cli_grant_set_list_show_remove_and_the_unbrokered_note():
    r = _run(
        "--json", "broker", "grant", "set", "jobdoc", "authz_relations", "--max-per-minute", "5"
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["grant"]["max_calls_per_minute"] == 5
    rows = json.loads(_run("--json", "broker", "grant", "list").output)
    assert rows[0]["subject"] == "jobdoc" and rows[0]["tool"] == "authz_relations"
    shown = json.loads(_run("--json", "broker", "grant", "show", "jobdoc").output)
    assert shown["grants"][0]["max_calls_per_minute"] == 5
    assert _run("broker", "grant", "show", "nobody").exit_code == 1
    r = _run("--json", "broker", "grant", "remove", "jobdoc")
    assert r.exit_code == 0 and json.loads(r.output)["unbrokered"] is True
    assert _run("--json", "broker", "grant", "remove", "jobdoc").exit_code == 1


def test_cli_invalid_grants_exit_1_and_store_nothing():
    r = _run("--json", "broker", "grant", "set", "jobdoc", "authz_relations", "--effect", "maybe")
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "invalid_grant"
    r = _run("--json", "broker", "grant", "set", "jobdoc", "nope_tool")
    assert r.exit_code == 1
    r = _run(
        "--json",
        "broker",
        "grant",
        "set",
        "jobdoc",
        "authz_relations",
        "--arg-schema-json",
        "{not json",
    )
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "invalid_json"
    r = _run(
        "--json", "broker", "grant", "set", "jobdoc", "authz_relations", "--credential", "noequals"
    )
    assert r.exit_code == 1
    assert store.list_grants() == []


def test_cli_simulate_exit_codes_and_json():
    assert _run("broker", "simulate", "--agent", "jobdoc", "--tool", "list_models").exit_code == 0
    _run("broker", "grant", "set", "jobdoc", "authz_relations")
    r = _run("--json", "broker", "simulate", "--agent", "jobdoc", "--tool", "recent_audit_events")
    assert r.exit_code == 1 and json.loads(r.output)["code"] == "no_grant"
    r = _run("--json", "broker", "simulate", "--agent", "jobdoc", "--tool", "authz_relations")
    assert r.exit_code == 0 and json.loads(r.output)["effect"] == "allow"
    bad = _run("--json", "broker", "simulate", "--agent", "a", "--tool", "t", "--args-json", "[1]")
    assert bad.exit_code == 1


def test_cli_grant_change_passes_through_the_policy_hook_and_is_audited(monkeypatch):
    import examlops.policy as policy

    rules = [{"name": "freeze-grants", "action": "tool_grant_change", "effect": "deny"}]
    monkeypatch.setattr(policy, "_load_policies", lambda path=None: rules)
    r = _run("broker", "grant", "set", "jobdoc", "authz_relations")
    assert r.exit_code == 1 and "freeze-grants" in r.output
    assert store.list_grants() == []
    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])
    assert _run("broker", "grant", "set", "jobdoc", "authz_relations").exit_code == 0
    assert _events("tool_grant_set")
    _run("--yes", "broker", "grant", "remove", "jobdoc")
    assert _events("tool_grant_removed")


# ── a lost audit event is counted, not hidden ─────────────────────────────────────────────


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_decision_audit_is_counted(monkeypatch):
    _break_audit(monkeypatch)
    assert (
        tb.invoke(AGENT, "authz_relations", {})["ok"] is True
    )  # fails open, like the ratchet says
    assert dropped_audit_events().get("tool_broker:allow") == 1


def test_a_lost_grant_change_audit_is_counted(monkeypatch):
    _break_audit(monkeypatch)
    _grant("authz_relations")
    assert dropped_audit_events().get("tool_grant_set") == 1
    tb.remove_grant("jobdoc")
    assert dropped_audit_events().get("tool_grant_removed") == 1
