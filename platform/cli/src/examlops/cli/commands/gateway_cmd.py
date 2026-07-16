"""B2 — `exa gateway`: virtual-key admin + a test chat call (ADR 0010)."""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Model gateway — virtual keys, routing, and cost",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa gateway key issue --tenant acme --project chat --budget 50 --model gpt-judge\n\n"
    "  exa gateway key list\n\n"
    "  exa gateway key revoke <key-hash>\n\n"
    "  exa gateway chat default --message 'hello there'"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


key_app = typer.Typer(
    help="Virtual key administration",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(key_app, name="key")


@key_app.command("issue", epilog=_EXAMPLES)
def key_issue(
    tenant: str = typer.Option("default", "--tenant", help="Tenant the key belongs to"),
    project: str = typer.Option("default", "--project", help="Project the key belongs to"),
    model: list[str] = typer.Option(
        None, "--model", help="Allow-list model (repeatable; omit = all models)"
    ),
    budget: float | None = typer.Option(None, "--budget", help="Budget in USD (omit = unlimited)"),
) -> None:
    """Issue a virtual key (printed once — only its hash is stored)."""
    from examlops.gateway import issue_virtual_key

    raw = issue_virtual_key(tenant, project, list(model) if model else None, budget, _actor())
    if _output.json_mode:
        _output.print_json({"virtual_key": raw, "tenant": tenant, "project": project})
    else:
        _output.ok(f"Issued virtual key for {tenant}/{project} (save it now — shown once):")
        typer.echo(raw)


@key_app.command("list")
def key_list() -> None:
    """List virtual keys (hashes only)."""
    from examlops.platform_db import list_virtual_keys

    keys = list_virtual_keys()
    if _output.json_mode:
        _output.print_json(keys)
        return
    if not keys:
        _output.ok("No virtual keys issued.")
        return
    rows = [
        [
            k["key_hash"][:16] + "…",
            f"{k['tenant']}/{k['project']}",
            ",".join(k["models"]) or "all",
            "∞" if k["budget_usd"] is None else f"${k['budget_usd']:.2f}",
            f"${k['spent_usd']:.4f}",
            "revoked" if k["revoked"] else "active",
        ]
        for k in keys
    ]
    _output.print_table(
        "Virtual Keys",
        ["Key (hash)", "Tenant/Project", "Models", "Budget", "Spent", "Status"],
        rows,
    )


@key_app.command("revoke")
def key_revoke(key_hash: str = typer.Argument(..., help="Key hash prefix or full hash")) -> None:
    """Revoke a virtual key by its stored hash."""
    from examlops.platform_db import list_virtual_keys, revoke_virtual_key, write_audit_event

    matches = [k for k in list_virtual_keys() if k["key_hash"].startswith(key_hash)]
    if not matches:
        _output.error(f"No key matching hash '{key_hash}'")
    if len(matches) > 1:
        _output.error(f"Ambiguous hash '{key_hash}' matches {len(matches)} keys")
    kh = matches[0]["key_hash"]
    revoke_virtual_key(kh)
    write_audit_event("exa-gateway", _actor(), "virtual_key_revoked", kh, None)
    _output.ok(f"Revoked key {kh[:16]}…")


cache_app = typer.Typer(
    help="Semantic cache (B3) — hit-rate + measured savings",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats_cmd(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter to one tenant"),
) -> None:
    """Show semantic-cache hit-rate and token/cost savings (B3)."""
    from examlops.platform_db import cache_stats

    stats = cache_stats(tenant)
    if _output.json_mode:
        _output.print_json(stats)
        return
    _output.print_table(
        "Semantic Cache" + (f" — {tenant}" if tenant else ""),
        ["Hits", "Misses", "Hit rate", "Tokens saved", "Cost saved"],
        [
            [
                str(stats["hits"]),
                str(stats["misses"]),
                f"{stats['hit_rate'] * 100:.1f}%",
                str(stats["tokens_saved"]),
                f"${stats['cost_saved']:.6f}",
            ]
        ],
    )


@app.command("chat", epilog=_EXAMPLES)
def chat(
    model: str = typer.Argument("default", help="Logical model name to route"),
    message: str = typer.Option(..., "--message", help="User message"),
    key: str | None = typer.Option(None, "--key", help="Virtual key to authenticate with"),
    cache: bool = typer.Option(False, "--cache", help="Route through the B3 semantic cache"),
) -> None:
    """Send one chat message through the gateway (uses the default echo route)."""
    from examlops.gateway import GatewayClient, GatewayError, build_default_router

    cache_lookup = cache_store = None
    if cache:
        from examlops.semantic_cache import SemanticCache, bind_to_gateway

        cache_lookup, cache_store = bind_to_gateway(SemanticCache())
    client = GatewayClient(
        build_default_router(), virtual_key=key, cache_lookup=cache_lookup, cache_store=cache_store
    )
    try:
        comp = client.chat(model, [{"role": "user", "content": message}])
    except GatewayError as exc:
        _output.error(f"{type(exc).__name__}: {exc}")
        return
    if _output.json_mode:
        _output.print_json(
            {
                "text": comp.text,
                "backend": comp.backend,
                "cost_usd": comp.cost_usd,
                "cached": comp.cached,
            }
        )
        return
    tag = " (cached)" if comp.cached else ""
    _output.ok(f"[{comp.backend}] {comp.text}  (cost ${comp.cost_usd:.6f}){tag}")


# ── B8: structured output + reasoning ops (ADR 0035) ──────────────────────────
schema_app = typer.Typer(no_args_is_help=True, help="Structured output — schema-constrained (B8)")
app.add_typer(schema_app, name="schema")

reasoning_app = typer.Typer(
    no_args_is_help=True, help="Reasoning ops — budget/accounting/trace (B8)"
)
app.add_typer(reasoning_app, name="reasoning")


@schema_app.command("test")
def schema_test(
    schema_file: str = typer.Argument(..., help="Path to a JSON Schema file"),
    object_file: str = typer.Argument(..., help="Path to a JSON object to validate"),
    repair: bool = typer.Option(True, "--repair/--no-repair", help="Attempt repair on invalid"),
) -> None:
    """Validate (and optionally repair) an object against a JSON Schema (R1/R8)."""
    import json as _json

    from examlops.structured import repair_object, validate_object

    with open(schema_file) as fh:
        schema = _json.load(fh)
    with open(object_file) as fh:
        obj = _json.load(fh)
    errors = validate_object(obj, schema)
    if not errors:
        _output.ok("Object is valid against the schema.")
        return
    if repair:
        fixed = repair_object(obj, schema)
        if not validate_object(fixed, schema):
            _output.warning("Invalid — repaired to a valid object:")
            _output.info(_json.dumps(fixed, indent=2))
            return
    _output.error(f"Invalid: {'; '.join(errors)}")
    raise typer.Exit(1)


@reasoning_app.command("account")
def reasoning_account(
    model: str = typer.Argument(..., help="Model name"),
    reasoning_tokens: int = typer.Option(..., "--reasoning", help="Reasoning (thinking) tokens"),
    output_tokens: int = typer.Option(..., "--output", help="Output tokens"),
    reasoning_rate: float = typer.Option(0.0, "--reasoning-rate", help="$/reasoning token"),
    output_rate: float = typer.Option(0.0, "--output-rate", help="$/output token"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Account reasoning vs output tokens/cost separately (R5)."""
    from examlops.structured import account_reasoning

    result = account_reasoning(
        model,
        reasoning_tokens,
        output_tokens,
        reasoning_rate=reasoning_rate,
        output_rate=output_rate,
        tenant=tenant,
    )
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(
        f"{model}: reasoning {result['reasoning_tokens']}tok (${result['reasoning_cost']}) + "
        f"output {result['output_tokens']}tok (${result['output_cost']}) = ${result['total_cost']}"
    )


@reasoning_app.command("budget")
def reasoning_budget(
    requested: int = typer.Argument(..., help="Requested thinking tokens"),
    max_thinking: int = typer.Option(..., "--max", help="Reasoning budget (max thinking tokens)"),
) -> None:
    """Show how a reasoning budget caps a request (R4)."""
    from examlops.structured import ReasoningBudget

    allowed, cut = ReasoningBudget(max_thinking).enforce(requested)
    if _output.json_mode:
        _output.print_json({"requested": requested, "allowed": allowed, "cut": cut})
        return
    if cut:
        _output.warning(f"Thinking cut off at {allowed} tokens (requested {requested}).")
    else:
        _output.ok(f"Within budget: {allowed} thinking tokens.")


@reasoning_app.command("stats")
def reasoning_stats(
    model: str = typer.Option(None, "--model", help="Filter by model"),
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
) -> None:
    """Reasoning-vs-output token/cost split + structured-output outcomes."""
    from examlops.platform_db import reasoning_usage_summary, structured_output_stats

    summary = reasoning_usage_summary(model, tenant)
    outcomes = structured_output_stats()
    if _output.json_mode:
        _output.print_json({"reasoning": summary, "structured_output": outcomes})
        return
    _output.print_record(
        {
            "reasoning_tokens": summary["reasoning_tokens"],
            "output_tokens": summary["output_tokens"],
            "reasoning_cost": f"${summary['reasoning_cost']:.6f}",
            "output_cost": f"${summary['output_cost']:.6f}",
            "structured_valid": outcomes.get("valid", 0),
            "structured_repaired": outcomes.get("repaired", 0),
            "structured_failed": outcomes.get("failed", 0),
        }
    )
