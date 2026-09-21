"""`exa broker` - the agent tool broker: grants and a dry-run of its decision (ADR 0145).

A *grant* says which tool a subject (an agent name, an agent-version id or a workload identity) may
call, and under which constraints. A subject with no grants is not brokered (default allow); one
with grants is default-deny. ``EXAMLOPS_TOOL_BROKER=monitor|enforce`` makes the MCP server consult
the grants; the default ``off`` leaves it byte-identical. The logic is :mod:`examlops.tool_broker`.

Exit codes: 0 ok (``simulate``: allow or require_approval), 1 refused (invalid grant, policy deny,
unknown subject; ``simulate``: deny).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from examlops import tool_broker as tb
from examlops.cli import _output
from examlops.cli._policy_gate import enforce_and_confirm

_H = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    help="Agent tool broker - which agent may call which tool (ADR 0145)",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)
grant_app = typer.Typer(
    help="Tool grants - per-subject allow/deny with constraints",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings=_H,
)
app.add_typer(grant_app, name="grant")

_EX_SET = (
    "Examples:\n\n"
    "  exa broker grant set jobdoc list_models\n\n"
    "  exa broker grant set jobdoc set_traffic_split --tier-ceiling A --needs-approval "
    "--max-per-minute 5\n\n"
    "  exa broker grant set jobdoc dataplane_pull --arg-schema-json "
    '\'{"type":"object","additionalProperties":false,'
    '"properties":{"name":{"type":"string","pattern":"pm100-.*"}}}\'\n\n'
    "  exa broker grant set jobdoc '*' --effect deny      # keep a subject locked out\n\n"
    "A subject with no grants is not brokered (default allow); with grants it is default-deny."
)
_EX_LIST = (
    "Examples:\n\n  exa broker grant list\n\n  exa -o json broker grant list --subject jobdoc"
)
_EX_SHOW = "Examples:\n\n  exa broker grant show jobdoc"
_EX_REMOVE = (
    "Examples:\n\n  exa broker grant remove jobdoc list_models\n\n"
    "  exa --yes broker grant remove jobdoc      # every grant: the subject is un-brokered again"
)
_EX_SIM = (
    "Examples:\n\n  exa broker simulate --agent jobdoc --tool list_models\n\n"
    "  exa -o json broker simulate --agent jobdoc --tool set_traffic_split "
    '--args-json \'{"model":"jpcp","production":100}\'\n\n'
    "Exit codes: 0 allow / require_approval, 1 deny. Nothing runs and no quota is counted."
)


def _fail(code: str, error: str, **extra: Any) -> None:
    if _output.json_mode:
        _output.print_json({"ok": False, "code": code, "error": error, **extra})
        raise typer.Exit(1)
    _output.error(error, hint=code)


def _json_opt(text: str | None, what: str) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        _fail("invalid_json", f"{what} is not valid JSON: {exc}")
        return None


@grant_app.command("set", epilog=_EX_SET)
def set_cmd(
    subject: str = typer.Argument(..., help="Agent name, agent-version id or workload subject"),
    tool: str = typer.Argument(..., help="A registered tool name, or '*' for all tools"),
    effect: str = typer.Option("allow", "--effect", help="allow | deny"),
    tier_ceiling: str | None = typer.Option(None, "--tier-ceiling", help="read | A | B | C"),
    needs_approval: bool = typer.Option(False, "--needs-approval", help="Human approval per call"),
    max_per_minute: int | None = typer.Option(None, "--max-per-minute", min=1),
    max_per_session: int | None = typer.Option(None, "--max-per-session", min=1),
    arg_schema_json: str | None = typer.Option(
        None, "--arg-schema-json", help="JSON-Schema subset the call arguments must satisfy"
    ),
    secret_bind: list[str] = typer.Option(
        [], "--credential", help="PARAM=SECRET_NAME injected at call time (repeatable)"
    ),
    egress_url_arg: list[str] = typer.Option([], "--egress-url-arg", help="URL-valued argument"),
    egress_host: list[str] = typer.Option([], "--egress-host", help="Allowed host (or *.suffix)"),
    from_file: Path | None = typer.Option(None, "--from-file", help="Grant document (JSON/YAML)"),
) -> None:
    """Create or replace one grant. Validated before it is stored; audited; policy-gated."""
    if from_file is not None:
        import yaml

        try:
            doc = yaml.safe_load(from_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _fail("unreadable_grant", f"cannot read {from_file}: {exc}")
            return
    else:
        doc = {"effect": effect}
        if tier_ceiling:
            doc["tier_ceiling"] = tier_ceiling
        if needs_approval:
            doc["needs_approval"] = True
        if max_per_minute:
            doc["max_calls_per_minute"] = max_per_minute
        if max_per_session:
            doc["max_calls_per_session"] = max_per_session
        schema = _json_opt(arg_schema_json, "--arg-schema-json")
        if schema is not None:
            doc["arg_schema"] = schema
        if secret_bind:
            creds: dict[str, str] = {}
            for item in secret_bind:
                name, sep, path = item.partition("=")
                if not sep or not name or not path:
                    _fail(
                        "invalid_credential", f"--credential {item!r}: expected PARAM=secret/path"
                    )
                    return
                creds[name] = path
            doc["credentials"] = creds
        if egress_url_arg or egress_host:
            doc["egress"] = {"url_args": list(egress_url_arg), "allowed_hosts": list(egress_host)}
    if not enforce_and_confirm(
        "tool_grant_change",
        {
            "subject": subject,
            "tool": tool,
            "effect": doc.get("effect") if isinstance(doc, dict) else None,
        },
        what=f"changing the grant of {subject} for {tool}",
        prompt=f"Set the grant of {subject} for {tool}?",
    ):
        return
    try:
        out = tb.set_grant(subject, tool, doc)
    except tb.GrantError as exc:
        _fail("invalid_grant", "invalid grant", problems=exc.problems)
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"{subject} / {tool}: grant {'replaced' if out['replaced'] else 'created'}")


@grant_app.command("list", epilog=_EX_LIST)
def list_cmd(
    subject: str | None = typer.Option(None, "--subject", "-s", help="Only this subject"),
) -> None:
    """List grants, grouped by subject."""
    rows = tb.list_grants(subject)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No tool grants: every caller is un-brokered (default allow)")
        return
    _output.print_table(
        "Tool grants",
        ["subject", "tool", "effect", "tier_ceiling", "approval", "per_min", "per_session"],
        [
            [
                r["subject"],
                r["tool"],
                r["effect"],
                r.get("tier_ceiling") or "",
                str(bool(r.get("needs_approval"))),
                str(r.get("max_calls_per_minute") or ""),
                str(r.get("max_calls_per_session") or ""),
            ]
            for r in rows
        ],
    )


@grant_app.command("show", epilog=_EX_SHOW)
def show_cmd(subject: str = typer.Argument(..., help="Agent name, version id or subject")) -> None:
    """Show every grant of one subject in full (credential paths, arg schema, egress)."""
    rows = tb.list_grants(subject)
    if not rows:
        _fail("not_found", f"{subject} has no grants (it is un-brokered: default allow)")
        return
    if _output.json_mode:
        _output.print_json({"subject": subject, "grants": rows})
        return
    for r in rows:
        _output.print_record(
            {
                k: (json.dumps(v, sort_keys=True) if isinstance(v, dict | list) else v)
                for k, v in r.items()
            }
        )


@grant_app.command("remove", epilog=_EX_REMOVE)
def remove_cmd(
    subject: str = typer.Argument(..., help="Agent name, version id or subject"),
    tool: str | None = typer.Argument(None, help="One tool; omit to remove every grant"),
) -> None:
    """Remove a grant (or all of a subject's). No grants left means default allow again."""
    if not enforce_and_confirm(
        "tool_grant_change",
        {"subject": subject, "tool": tool or "*", "remove": True},
        what=f"removing grants of {subject}",
        prompt=f"Remove {'the ' + tool + ' grant' if tool else 'every grant'} of {subject}?",
    ):
        return
    out = tb.remove_grant(subject, tool)
    if out["removed"] == 0:
        _fail("not_found", f"no such grant for {subject}")
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    tail = " - the subject is now un-brokered (default allow)" if out["unbrokered"] else ""
    _output.ok(f"removed {out['removed']} grant(s) of {subject}{tail}")


@app.command("simulate", epilog=_EX_SIM)
def simulate_cmd(
    agent: str = typer.Option(..., "--agent", help="Agent name"),
    tool: str = typer.Option(..., "--tool", help="Tool name"),
    args_json: str = typer.Option("{}", "--args-json", help="Call arguments as a JSON object"),
    version_id: str | None = typer.Option(None, "--version-id", help="Agent version id"),
    subject: str | None = typer.Option(None, "--subject", help="Workload identity subject"),
    session: str | None = typer.Option(None, "--session", help="Session id"),
) -> None:
    """What the broker would decide for one call - runs nothing, counts no quota."""
    args = _json_opt(args_json, "--args-json")
    if not isinstance(args, dict):
        _fail("invalid_json", "--args-json must be a JSON object")
        return
    caller = tb.ToolCaller(agent=agent, version_id=version_id, subject=subject, session=session)
    out = tb.simulate(caller, tool, args)
    if _output.json_mode:
        _output.print_json(out)
    else:
        _output.print_record(
            {
                "effect": out["effect"],
                "code": out["code"],
                "reason": out["reason"],
                "grant_subject": out["grant_subject"] or "(none: un-brokered)",
                "tier": out["tier"],
                "problems": "; ".join(out["problems"]),
            }
        )
    if out["effect"] == "deny":
        raise typer.Exit(1)
