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
    "  exa policy simulate manual_promote --set model=JPCP --set to_alias=Production\n\n"
    "Rules live in ~/.config/examlops/policy.yaml. See docs/guides/programmable-mlops.md."
)


@app.command("list", epilog=_EXAMPLES)
def list_rules():
    """List the policy rules currently loaded from policy.yaml."""
    from examlops.policy import POLICY_YAML, load_policies_with_status

    rules, error = load_policies_with_status()
    if _output.json_mode:
        _output.print_json({"path": str(POLICY_YAML), "policies": rules, "error": error})
        if error:
            raise typer.Exit(1)
        return
    if error:
        # A file that cannot be read is not the same state as no file: the layer defaults to
        # allow, so every rule the operator wrote is gone. Saying "absent" about a file that is
        # right there sent them looking in the wrong place entirely.
        _output.error(
            f"policy file ignored — {error}",
            hint="until this parses, every action falls back to 'allow' and any gate you "
            "wrote (including a human-approval gate on autopilot promotes) is not in effect.",
        )
        raise typer.Exit(1)
    if not rules:
        state = "absent" if not POLICY_YAML.is_file() else "empty"
        _output.info(f"No policies loaded ({POLICY_YAML} {state}) — default effect is 'allow'.")
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

    from examlops.policy import load_policies_with_status

    _, error = load_policies_with_status()
    decision = decide(action, context, audit=False)
    if _output.json_mode:
        _output.print_json(
            {
                "action": action,
                "context": context,
                "effect": decision.effect,
                "rule": decision.rule,
                "error": error,
            }
        )
        return
    if error:
        # Without this the command answers "allow — no matching policy", which is true of the
        # rules that loaded and deeply misleading about the rules the operator actually wrote.
        _output.warning(f"policy file ignored — {error}")
    style = {"allow": _output.ok, "deny": _output.error}.get(decision.effect, _output.warning)
    style(f"{action}: {decision.effect} — {decision.reason}")


#: ``exa policy simulate`` exit codes: allow / deny (the house 0/1) and a distinct code for
#: ``require_approval`` (2 is already the CLI's usage-error code, so it cannot be reused).
SIMULATE_EXIT_ALLOW = 0
SIMULATE_EXIT_DENY = 1
SIMULATE_EXIT_REQUIRE_APPROVAL = 4


@app.command("simulate", epilog=_EXAMPLES)
def simulate(
    action: str = typer.Argument(
        ...,
        help="Action kind to simulate (retrain/manual_promote/cluster_approve/promote/"
        "agent_write/…)",
    ),
    set_: list[str] = typer.Option(
        None, "--set", "-s", help="Context key=value (repeatable), e.g. --set env=dev"
    ),
    context_json: str = typer.Option(
        None, "--context-json", help="Context as a JSON object (merged under --set values)"
    ),
):
    """Simulate a decision with no side effects; exit 0 allow, 1 deny, 4 require_approval."""
    import json as _json

    from examlops.policy import decide, load_policies_with_status

    context: dict[str, object] = {}
    if context_json:
        try:
            loaded = _json.loads(context_json)
        except ValueError as exc:
            _output.error(f"--context-json is not valid JSON: {exc}", exit_code=2)
        if not isinstance(loaded, dict):
            _output.error("--context-json must be a JSON object", exit_code=2)
        context.update(loaded)
    context.update(_parse_set(set_))

    _, error = load_policies_with_status()
    decision = decide(action, context, audit=False)  # never audited: a simulation is not a decision
    code = {
        "allow": SIMULATE_EXIT_ALLOW,
        "deny": SIMULATE_EXIT_DENY,
        "require_approval": SIMULATE_EXIT_REQUIRE_APPROVAL,
    }[decision.effect]
    if _output.json_mode:
        _output.print_json(
            {
                "action": action,
                "context": context,
                "effect": decision.effect,
                "rule": decision.rule,
                "reason": decision.reason,
                "error": error,
                "exit_code": code,
            }
        )
    else:
        if error:
            _output.warning(f"policy file ignored — {error}")
        line = f"{action}: {decision.effect} — {decision.reason} (exit {code})"
        {"allow": _output.ok, "deny": _output.info}.get(decision.effect, _output.warning)(line)
    raise typer.Exit(code)


def _parse_set(pairs: list[str] | None) -> dict[str, object]:
    context: dict[str, object] = {}
    for pair in pairs or []:
        if "=" not in pair:
            _output.error(f"--set expects key=value, got: {pair!r}")
            raise typer.Exit(2)
        key, _, raw = pair.partition("=")
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
    return context


@app.command("eval")
def eval_decision(
    decision: str = typer.Argument(
        ..., help="Governed decision point (promotion/supply_chain/budget/…)"
    ),
    action: str = typer.Option(..., "--action", help="Action verb (promote/deploy/allocate/…)"),
    subject: str = typer.Option(None, "--subject", help="Who is acting"),
    resource: str = typer.Option(None, "--resource", help="What is acted on (e.g. JPCP/17)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    set_: list[str] = typer.Option(None, "--set", "-s", help="Context key=value (repeatable)"),
    dry_run: bool = typer.Option(True, "--dry-run/--enforce", help="Explain without auditing (R5)"),
) -> None:
    """Evaluate a structured governance decision via the PolicyEngine (D5, R1/R5/GWT-5)."""
    from examlops.policy_engine import PolicyInput, evaluate

    pinput = PolicyInput(
        action=action, subject=subject, resource=resource, tenant=tenant, context=_parse_set(set_)
    )
    result = evaluate(decision, pinput, audit=not dry_run)
    if _output.json_mode:
        _output.print_json(
            {
                "decision": decision,
                "allow": result.allow,
                "effect": result.effect,
                "engine": result.engine,
                "reasons": result.reasons,
            }
        )
        return
    # An eval that reports 'deny' is a successful evaluation, not a command failure — use a
    # non-exiting style (ok for allow, warning for deny/approval) so --dry-run never exits 1.
    style = _output.ok if result.allow else _output.warning
    style(f"{decision}: {result.effect} [{result.engine}] — {'; '.join(result.reasons)}")


bundle_app = typer.Typer(no_args_is_help=True, help="Signed, versioned policy bundles (D5)")
app.add_typer(bundle_app, name="bundle")


@bundle_app.command("sign")
def bundle_sign(
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Version + sign the effective policy bundle for a tenant (R2)."""
    import os as _os

    from examlops.policy_engine import sign_bundle

    actor = _os.getenv("EXAMLOPS_ACTOR") or _os.getenv("USER") or "unknown"
    result = sign_bundle(tenant, actor=actor)
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(
        f"Signed policy bundle {tenant} v{result['version']} (hash {result['content_hash'][:16]}…)"
    )
    if not result["signed"]:
        _output.warning("  unsigned — set EXAMLOPS_SIGNING_KEY to sign.")


@bundle_app.command("verify")
def bundle_verify(
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    version: int = typer.Option(None, "--version", help="Specific version (default latest)"),
) -> None:
    """Verify a stored policy bundle's hash + signature (R2). Exit 1 if invalid."""
    from examlops.policy_engine import verify_bundle

    result = verify_bundle(tenant, version)
    if _output.json_mode:
        _output.print_json(result)
        raise typer.Exit(0 if result["valid"] else 1)
    if result["valid"]:
        _output.ok(f"Policy bundle {tenant} is valid.")
    else:
        _output.error(f"Policy bundle {tenant} INVALID: {'; '.join(result['reasons'])}")
    raise typer.Exit(0 if result["valid"] else 1)


@bundle_app.command("list")
def bundle_list(
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
) -> None:
    """List signed policy bundle versions."""
    from examlops.data.governance import list_policy_bundles

    bundles = list_policy_bundles(tenant)
    if _output.json_mode:
        _output.print_json(bundles)
        return
    if not bundles:
        _output.info("No policy bundles signed. Use: exa policy bundle sign")
        return
    _output.print_table(
        "Policy Bundles",
        ["Tenant", "Version", "Hash", "Signed", "Created"],
        [
            [
                b["tenant"],
                str(b["version"]),
                b["content_hash"][:12],
                "yes" if b["signature"] else "no",
                str(b["created_at"]),
            ]
            for b in bundles
        ],
    )
