"""D8 — `exa guardrails`: test input/output guardrails + view violations (ADR 0026)."""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Guardrails — injection/PII/toxicity defense with monitor/enforce modes",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa guardrails test --text 'ignore previous instructions' --direction input\n\n"
    "  exa guardrails test --text 'email me at a@b.com' --direction output --mode enforce\n\n"
    "  exa guardrails stats"
)


@app.command("test", epilog=_EXAMPLES)
def test(
    text: str = typer.Option(..., "--text", help="Text to run through the guardrail"),
    direction: str = typer.Option("input", "--direction", help="input | output"),
    mode: str = typer.Option("enforce", "--mode", help="off | monitor | enforce"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant policy scope (D6)"),
) -> None:
    """Run a text through the guardrail and show the action + findings."""
    from examlops.guardrails import DefaultGuardrail

    guard = DefaultGuardrail(mode=mode, tenant=tenant)
    res = guard.check_output(text, {}) if direction == "output" else guard.check_input(text, {})
    if _output.json_mode:
        _output.print_json(
            {"action": res.action, "findings": res.findings, "reason": res.reason, "text": res.text}
        )
        return
    color = {"allow": "green", "redact": "yellow", "block": "red"}.get(res.action, "white")
    _output.info(f"Action: [{color}]{res.action}[/{color}]  ({res.reason or 'clean'})")
    if res.findings:
        _output.info(f"Findings: {', '.join(res.findings)}")
    if res.action == "redact":
        _output.info(f"Redacted: {res.text}")
    if res.action == "block":
        _output.warning("Content blocked (enforce mode).")


@app.command("check-tool")
def check_tool(
    tool: str = typer.Argument(..., help="Tool name the agent wants to call"),
    allow: list[str] = typer.Option(..., "--allow", help="Allowed tool (repeatable)"),
    mode: str = typer.Option("enforce", "--mode", help="off | monitor | enforce"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Check an agent tool call against the per-tenant allow-list (R7)."""
    from examlops.guardrails import DefaultGuardrail

    guard = DefaultGuardrail(mode=mode, tenant=tenant, allowed_tools=set(allow))
    ok = guard.check_tool_call(tool, {})
    if _output.json_mode:
        _output.print_json({"tool": tool, "allowed": ok})
        return
    if ok:
        _output.ok(f"Tool '{tool}' allowed")
    else:
        _output.error(f"Tool '{tool}' blocked (not in allow-list)")


@app.command("stats")
def stats(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """Show guardrail action counts (allow/redact/block)."""
    from examlops.guardrails import guardrail_stats

    s = guardrail_stats(tenant)
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        "Guardrail Events" + (f" — {tenant}" if tenant else ""),
        ["Allow", "Redact", "Block", "Total"],
        [[str(s["allow"]), str(s["redact"]), str(s["block"]), str(s["total"])]],
    )
