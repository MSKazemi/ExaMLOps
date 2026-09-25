"""ADR 0079 decision 2 on the CLI — every mutating ``exa`` command consults ``policy.decide``.

The hook (``examlops.cli._policy_hook``) makes "gated" the default for every admin, destructive
and cli-only command, so these tests do two things:

* **Behaviour** — through the real CLI runner, the real policy engine, a real policy file under
  ``tmp_path`` and the real audit log: no rule is byte-identical (no audit row), deny blocks
  before the body runs, require_approval asks and a decline changes nothing, monitor observes,
  an engine failure fails closed, ``--dry-run`` is never blocked, secrets never reach the audit.
* **Forcing guard** — the tables name only commands that exist, every ``self`` entry's module
  really calls the policy layer, derived action names do not collide, and every non-read leaf
  of the live tree is covered (gated, self-gated or exempt with a reason).
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli import _policy_hook, surface  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY", raising=False)
    from examlops import platform_db

    platform_db.init_db()
    return tmp_path


def _policy(tmp_path, monkeypatch, text: str | None) -> Path:
    path = tmp_path / "policy.yaml"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", path)
    return path


def _events(prefix: str) -> list[dict]:
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if str(e["action"]).startswith(prefix)]


def _details(row: dict) -> dict:
    d = row["details"]
    return json.loads(d) if isinstance(d, str) else d


def _granted(subject: str, obj: str) -> bool:
    from examlops import authz

    return any(o.get("object") == obj for o in authz.list_objects(subject))


def _grant(*extra: str, answer: str | None = None):
    return runner.invoke(
        app, [*extra, "project", "grant", "alice", "editor", "project:acme"], input=answer
    )


# ── behaviour ───────────────────────────────────────────────────────────────────────────────
def test_no_policy_file_is_byte_identical(env, monkeypatch):
    _policy(env, monkeypatch, None)
    res = _grant()
    assert res.exit_code == 0, res.output
    assert _granted("alice", "project:acme")
    assert _events("policy") == []  # no decision row when no rule applies


def test_rule_for_another_action_does_not_touch_this_command(env, monkeypatch):
    _policy(env, monkeypatch, "policies:\n  - action: project_revoke\n    effect: deny\n")
    res = _grant()
    assert res.exit_code == 0, res.output
    assert _granted("alice", "project:acme")
    assert _events("policy") == []


def test_deny_blocks_before_the_body_runs_and_is_audited(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - name: no-grants\n    action: project_grant\n    effect: deny\n",
    )
    res = _grant()
    assert res.exit_code == 1
    assert "no-grants" in res.output
    assert not _granted("alice", "project:acme")  # the body never ran
    rows = _events("policy:project_grant")
    assert len(rows) == 1
    assert _details(rows[0])["effect"] == "deny"


def test_condition_reads_cli_parameters(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n"
        "  - action: project_grant\n"
        "    when: \"relation == 'owner'\"\n"
        "    effect: deny\n",
    )
    assert _grant().exit_code == 0  # editor → no match
    res = runner.invoke(app, ["project", "grant", "bob", "owner", "project:acme"])
    assert res.exit_code == 1
    assert not _granted("bob", "project:acme")


def test_require_approval_declined_changes_nothing(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    res = _grant(answer="n\n")
    assert res.exit_code == 1
    assert "did not run" in res.output
    assert not _granted("alice", "project:acme")


def test_require_approval_confirmed_runs(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    res = _grant(answer="y\n")
    assert res.exit_code == 0, res.output
    assert _granted("alice", "project:acme")
    assert _details(_events("policy:project_grant")[0])["effect"] == "require_approval"


def test_structured_output_is_not_approval(env, monkeypatch):
    """`-o json` makes `_output.confirm` auto-yes for a human's script; a require_approval rule
    must not be satisfied by an output format — only an explicit --yes or an answered prompt."""
    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    res = _grant("-o", "json")
    assert res.exit_code == 1, res.output
    assert not _granted("alice", "project:acme")
    res = _grant("-o", "json", "--yes")
    assert res.exit_code == 0, res.output
    assert _granted("alice", "project:acme")


def test_require_approval_refuses_an_agent_principal(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")
    res = _grant("--yes")
    assert res.exit_code == 1
    assert "plan_required" in res.output
    assert not _granted("alice", "project:acme")


def test_monitor_rule_never_blocks_but_is_audited(env, monkeypatch):
    _policy(
        env,
        monkeypatch,
        "policies:\n  - name: watch\n    action: project_grant\n    effect: deny\n"
        "    mode: monitor\n",
    )
    res = _grant()
    assert res.exit_code == 0, res.output
    assert _granted("alice", "project:acme")
    rows = _events("policy_monitor:project_grant")
    assert rows and _details(rows[0])["would_effect"] == "deny"


def test_engine_failure_fails_closed(env, monkeypatch):
    _policy(env, monkeypatch, None)

    def boom(*_a, **_k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr("examlops.policy.decide", boom)
    res = _grant()
    assert res.exit_code == 1
    assert not _granted("alice", "project:acme")
    assert _events("policy_unavailable:project_grant")


def test_dry_run_is_never_blocked(env, monkeypatch):
    _policy(env, monkeypatch, "policies:\n  - action: approval_approve\n    effect: deny\n")
    res = runner.invoke(app, ["approvals", "approve", "JPCP", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "would approve" in res.output.lower()


def test_shared_action_name_and_ack_reaches_the_control_plane(env, monkeypatch):
    """`exa approvals approve` decides as `approval_approve` — the dashboard's and the control
    plane's name — and an approved decision travels as the acknowledgement header."""
    _policy(
        env, monkeypatch, "policies:\n  - action: approval_approve\n    effect: require_approval\n"
    )
    seen: dict = {}

    def fake_approve(model, **_):
        from examlops.policy import http_gate

        seen["model"] = model
        seen["ack"] = http_gate.approval_ack_active()
        return {"flow_run_id": "fr-1", "status": "scheduled"}

    with patch("examlops.cli.commands.approvals.control_plane_api.approve", fake_approve):
        res = runner.invoke(app, ["approvals", "approve", "JPCP"], input="y\ny\n")
    assert res.exit_code == 0, res.output
    assert seen == {"model": "JPCP", "ack": True}
    rows = _events("policy:approval_approve")
    assert rows and rows[0]["target"] == "JPCP"


def test_secret_values_never_enter_the_context():
    ctx = _policy_hook.build_context(
        "secrets set", "admin", {"name": "db", "value": "hunter2", "api_key": "k", "n": 3}
    )
    assert "hunter2" not in repr(ctx) and "api_key" not in ctx and "value" not in ctx
    assert ctx["name"] == "db" and ctx["n"] == 3 and ctx["command"] == "secrets set"


def test_a_credential_named_innocently_is_found_by_its_help():
    """`exa gateway chat --key` is a virtual key (a bearer credential): the name `key` escapes the
    name pattern, so the parameter's own metadata must keep it out of the decision context."""
    import typer.main

    root = typer.main.get_command(app)
    chat = root.commands["gateway"].commands["chat"]
    secret = _policy_hook.secret_params(chat)
    assert "key" in secret and "model" not in secret and "message" not in secret
    ctx = _policy_hook.build_context(
        "gateway chat", "admin", {"model": "default", "key": "vk-live-SECRET"}, secret
    )
    assert "vk-live-SECRET" not in repr(ctx)
    # Help that only *mentions* credentials keeps its vocabulary key for policy conditions.
    for cmd, name in (("dataplane sources create", "connection"), ("secrets set", "path")):
        node = root
        for part in cmd.split():
            node = node.commands[part]
        assert name not in _policy_hook.secret_params(node), (cmd, name)


def test_an_approval_is_itself_audited(env, monkeypatch):
    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    assert _grant(answer="y\n").exit_code == 0
    rows = _events("policy_approval:project_grant")
    assert len(rows) == 1
    d = _details(rows[0])
    assert d["approved_by"] == "tester" and d["consent"] == "prompt" and d["via"] == "cli"
    assert _grant("--yes").exit_code == 0
    consents = {_details(r)["consent"] for r in _events("policy_approval:project_grant")}
    assert consents == {"prompt", "--yes"}


def test_a_lost_approval_audit_is_counted_and_the_approved_run_goes_on(env, monkeypatch):
    from examlops.data import audit

    _policy(
        env, monkeypatch, "policies:\n  - action: project_grant\n    effect: require_approval\n"
    )
    audit.reset_dropped_audit_events()
    real = audit.write_audit_event

    def lose_approvals(source, actor, action, *a, **k):
        if str(action).startswith("policy_approval:"):
            raise OSError("audit datastore unavailable")
        return real(source, actor, action, *a, **k)

    monkeypatch.setattr(audit, "write_audit_event", lose_approvals)
    try:
        res = _grant(answer="y\n")
        assert res.exit_code == 0, res.output
        assert _granted("alice", "project:acme")
        assert audit.dropped_audit_events().get("policy_approval:project_grant") == 1
    finally:
        audit.reset_dropped_audit_events()


def test_parameters_cannot_impersonate_identity_keys(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ACTOR", "real")
    ctx = _policy_hook.build_context("x y", "admin", {"actor": "root", "command": "fake"})
    assert ctx["actor"] == "real" and ctx["command"] == "x y"


def test_context_is_bounded():
    params = {f"p{i}": "v" * 1000 for i in range(100)}
    ctx = _policy_hook.build_context("x", "admin", params)
    assert len([k for k in ctx if re.fullmatch(r"p\d+", k)]) <= _policy_hook._MAX_KEYS
    assert all(len(v) <= _policy_hook._MAX_LEN for v in ctx.values() if isinstance(v, str))


def test_vocabulary_keys_are_mapped():
    ctx = _policy_hook.build_context("serve traffic", "destructive", {"model_name": "JPCP"})
    assert ctx["model"] == "JPCP" and ctx["target"] == "JPCP"


def test_plugin_command_without_a_tier_is_gated():
    assert _policy_hook.classify("thirdparty doit", None) == ("gated", "thirdparty_doit")


# ── forcing guard ───────────────────────────────────────────────────────────────────────────
_LEAVES = dict(surface._leaves(surface.live_tree()))


@pytest.mark.parametrize("table", ["SELF_GATED", "EXEMPT", "ACTION_NAMES"])
def test_table_entries_name_live_commands(table):
    stale = sorted(set(getattr(_policy_hook, table)) - set(_LEAVES))
    assert not stale, f"{table} names commands that no longer exist: {stale}"


def test_no_read_tier_command_is_listed():
    listed = set(_policy_hook.SELF_GATED) | set(_policy_hook.EXEMPT)
    reads = sorted(p for p in listed if surface.TIERS.get(p) == surface.READ)
    assert not reads, f"read-tier commands need no entry: {reads}"


_POLICY_CALL = re.compile(r"_policy_gate|decide_safe|policy\.decide|_policy_decide")


def _delegate_sources(cb) -> str:
    """The callback's module source, plus any command module it imports and calls into."""
    fn = inspect.unwrap(cb)
    module = sys.modules[fn.__module__]
    sources = [inspect.getsource(module)]
    for name in re.findall(r"from examlops\.cli\.commands import (\w+)", inspect.getsource(fn)):
        sources.append(inspect.getsource(importlib.import_module(f"examlops.cli.commands.{name}")))
    return "\n".join(sources)


@pytest.mark.parametrize("path", sorted(_policy_hook.SELF_GATED))
def test_self_gated_commands_really_consult_policy(path):
    src = _delegate_sources(_LEAVES[path].callback)
    action = _policy_hook.SELF_GATED[path]
    assert _POLICY_CALL.search(src), f"{path} is listed as self-gated but never calls policy"
    assert f'"{action}"' in src, f"{path}: action {action!r} does not appear in its source"


def test_every_non_read_leaf_is_covered_and_actions_are_unique():
    by_action: dict[str, list[str]] = {}
    for path in _LEAVES:
        kind, detail = _policy_hook.classify(path, surface.TIERS.get(path))
        assert kind in {"gated", "self", "exempt", "read"}
        if kind == "exempt":
            assert detail.strip(), f"{path} is exempt without a reason"
        if kind == "gated":
            by_action.setdefault(detail, []).append(path)
    dupes = {a: p for a, p in by_action.items() if len(p) > 1}
    assert not dupes, f"two commands derive one action name: {dupes}"
    self_actions = set(_policy_hook.SELF_GATED.values())
    clash = sorted(set(by_action) & self_actions)
    assert not clash, f"a gated command reuses a self-gated command's action: {clash}"
    assert len(by_action) > 150  # the default really is "gated"


def test_the_hook_is_installed_on_the_live_tree(env, monkeypatch):
    """Every leaf the root group resolves carries the hook — no command escapes by construction."""
    import typer.main

    root = typer.main.get_command(app)
    ctx = root.make_context("exa", [], resilient_parsing=True)
    unhooked = []
    for name in root.list_commands(ctx):
        cmd = root.get_command(ctx, name)
        for path, leaf in surface._leaves(cmd):
            if leaf.callback is not None and not getattr(leaf, _policy_hook._HOOKED_ATTR, False):
                unhooked.append(f"{name} {path}".strip())
    assert not unhooked, unhooked
