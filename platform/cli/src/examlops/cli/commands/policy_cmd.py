"""``exa policy`` — inspect and test the declarative policy-as-code layer (ADR 0079).

``exa policy list`` shows the rules loaded from ``~/.config/examlops/policy.yaml``; ``exa policy test``
evaluates a decision for an action + context so an operator can dry-run a policy before trusting it
(the decision is *not* audited in test mode).
"""

from __future__ import annotations

import typer

from .. import _output

app = typer.Typer(
    no_args_is_help=True, help="Policy-as-code — declarative governance for mutations"
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa policy list\n\n"
    "  exa policy test retrain --set model=JPCP --set env=dev\n\n"
    "  exa policy test promote --set rmse_new=4.1 --set rmse_prod=5.0 --set env=dev\n\n"
    "Rules live in ~/.config/examlops/policy.yaml. See docs/guides/programmable-mlops.md."
)


@app.command("list", epilog=_EXAMPLES)
def list_rules():
    """List the policy rules currently loaded from policy.yaml."""
    from examlops.policy import POLICY_YAML, _load_policies

    rules = _load_policies()
    if _output.json_mode:
        _output.print_json({"path": str(POLICY_YAML), "policies": rules})
        return
    if not rules:
        _output.info(f"No policies loaded ({POLICY_YAML} absent) — default effect is 'allow'.")
        return
    rows = [
        [
            str(r.get("name") or f"#{i}"),
            str(r.get("action", "*")),
            str(r.get("when") or "—"),
            str(r.get("effect", "allow")),
        ]
        for i, r in enumerate(rules)
    ]
    _output.print_table("Policy rules", ["Name", "Action", "Condition", "Effect"], rows)


@app.command("test", epilog=_EXAMPLES)
def test(
    action: str = typer.Argument(
        ..., help="Action to evaluate (retrain/promote/connect_cluster/agent_write)"
    ),
    set_: list[str] = typer.Option(
        None, "--set", "-s", help="Context key=value (repeatable), e.g. --set env=dev"
    ),
):
    """Evaluate the policy decision for an action + context (not audited)."""
    from examlops.policy import decide

    context: dict[str, object] = {}
    for pair in set_ or []:
        if "=" not in pair:
            _output.error(f"--set expects key=value, got: {pair!r}")
            raise typer.Exit(2)
        key, _, raw = pair.partition("=")
        # Coerce bools/numbers so conditions (dummy == False, rmse_new < rmse_prod) match the real
        # call sites, which pass native Python types.
        if raw.lower() in {"true", "false"}:
            value: object = raw.lower() == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        context[key.strip()] = value

    decision = decide(action, context, audit=False)
    if _output.json_mode:
        _output.print_json(
            {"action": action, "context": context, "effect": decision.effect, "rule": decision.rule}
        )
        return
    style = {"allow": _output.ok, "deny": _output.error}.get(decision.effect, _output.warning)
    style(f"{action}: {decision.effect} — {decision.reason}")
