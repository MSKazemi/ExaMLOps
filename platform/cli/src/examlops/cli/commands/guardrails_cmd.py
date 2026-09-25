"""D8 — `exa guardrails`: test input/output guardrails + view violations (ADR 0026)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="off | monitor | enforce (default: the policy's mode, or enforce with no policy)",
    ),
    tenant: str = typer.Option("default", "--tenant", help="Tenant policy scope (D6)"),
    route: str | None = typer.Option(
        None, "--route", help="Route (model) whose per-route policy overrides apply"
    ),
) -> None:
    """Run a text through the guardrail and show the action + findings.

    Uses the declarative guardrail policy (EXAMLOPS_GUARDRAIL_POLICY or
    <config dir>/guardrails.yaml) when one is configured — the same checks the gateway runs for
    this tenant and route — else the built-in guardrail.
    """
    from examlops.guardrails import DefaultGuardrail
    from examlops.guardrails.policy import env_mode, policy_guardrail

    if mode is not None and mode not in ("off", "monitor", "enforce"):
        _output.error("--mode must be one of: off, monitor, enforce")
        raise typer.Exit(2)
    # Where the policy sets no mode, it inherits EXAMLOPS_GUARDRAIL_MODE — as in the gateway.
    guard: Any = policy_guardrail(tenant, fallback_mode=mode or env_mode())
    if guard is not None:
        guard.mode_override = mode
    else:
        guard = DefaultGuardrail(mode=mode or "enforce", tenant=tenant)
    ctx: dict[str, Any] = {"tenant": tenant}
    if route:
        ctx["route"] = route
    res = guard.check_output(text, ctx) if direction == "output" else guard.check_input(text, ctx)
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


@app.command("checks")
def checks() -> None:
    """List the checks a guardrail policy can compose, and whether each can run here."""
    from examlops.guardrails.policy import list_checks

    rows = list_checks()
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Guardrail checks",
        ["Check", "Kind", "Default action", "Available", "Description"],
        [
            [
                r["check"],
                "framework" if r["framework"] else "built-in",
                r["default_action"],
                "yes" if r["available"] else f"no — {r['reason']}",
                r["description"],
            ]
            for r in rows
        ],
    )


# ── declarative policy (ADR 0026 clause 3) ──────────────────────────────────────────────────

policy_app = typer.Typer(
    help="Declarative guardrail policy — per-tenant / per-route checks and modes",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(policy_app, name="policy")

_POLICY_EXAMPLES = (
    "Examples:\n\n"
    "  exa guardrails policy validate --file guardrails.yaml\n\n"
    "  exa guardrails policy show --tenant acme --route llama3.1:8b"
)


def _policy_path(file: Path | None) -> Path | None:
    from examlops.guardrails.policy import default_policy_path

    return file if file is not None else default_policy_path()


@policy_app.command("validate", epilog=_POLICY_EXAMPLES)
def policy_validate(
    file: Path | None = typer.Option(
        None, "--file", help="Policy YAML (default: the configured policy file)"
    ),
) -> None:
    """Validate a guardrail policy file; exit 1 on any error (a CI gate)."""
    from examlops.guardrails.policy import validate_policy_file

    path = _policy_path(file)
    if path is None:
        _output.error(
            "no guardrail policy configured — set EXAMLOPS_GUARDRAIL_POLICY or pass --file"
        )
        raise typer.Exit(1)
    report = validate_policy_file(path)
    if _output.json_mode:
        _output.print_json(report)
    elif report["valid"]:
        _output.ok(f"{path}: valid")
        for row in report["unavailable"]:
            _output.warning(
                f"check {row['check']!r} cannot run here ({row['reason']}) — enforce blocks, "
                "monitor records"
            )
    else:
        _output.error(f"{path}: invalid")
        for err in report["errors"]:
            _output.info(f"  - {err}")
    if not report["valid"]:
        raise typer.Exit(1)


@policy_app.command("show", epilog=_POLICY_EXAMPLES)
def policy_show(
    tenant: str = typer.Option("default", "--tenant", help="Tenant to resolve for"),
    route: str | None = typer.Option(None, "--route", help="Route (model) to resolve for"),
    file: Path | None = typer.Option(
        None, "--file", help="Policy YAML (default: the configured policy file)"
    ),
) -> None:
    """Show the effective policy for a tenant and route (layers: default → tenant → routes)."""
    from dataclasses import replace

    from examlops.guardrails.policy import (
        BUILTIN_DEFAULT,
        PolicyError,
        env_mode,
        load_policy_file,
    )

    path = _policy_path(file)
    if path is None:
        resolved = replace(BUILTIN_DEFAULT, mode=env_mode())
        source = "built-in (no policy file)"
    else:
        try:
            resolved = load_policy_file(path).resolve(tenant, route, base_mode=env_mode())
        except PolicyError as exc:
            _output.error(f"{path}: invalid — {exc}")
            raise typer.Exit(1) from exc
        source = str(path)
    doc = {"source": source, "tenant": tenant, "route": route, **resolved.as_dict()}
    if _output.json_mode:
        _output.print_json(doc)
        return
    _output.info(f"Policy: {source}  (layers: {' → '.join(resolved.layers)})")
    _output.info(f"Mode: {resolved.mode}")
    for direction in ("input", "output"):
        items = [f"{c.check}({c.action})" for c in resolved.checks(direction)]
        _output.info(f"{direction.capitalize()}: {', '.join(items) or '(none)'}")
    if resolved.banned_topics:
        _output.info(f"Banned topics: {', '.join(resolved.banned_topics)}")
    tools = "all" if resolved.allowed_tools is None else ", ".join(sorted(resolved.allowed_tools))
    _output.info(f"Allowed tools: {tools}")
    if resolved.blocked_tools:
        _output.info(f"Blocked tools: {', '.join(sorted(resolved.blocked_tools))}")
