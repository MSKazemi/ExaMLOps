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
    "  exa gateway chat default --message 'hello there'\n\n"
    "  # A model registered with `exa serve llm start` is a route under its own name\n"
    "  exa gateway chat qwen-vl --message 'Summarise this alert' --key $EXA_KEY"
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
    from examlops.data.gateway import list_virtual_keys

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
    from examlops.data.audit import write_audit_event
    from examlops.data.gateway import list_virtual_keys
    from examlops.data.governance import revoke_virtual_key

    matches = [k for k in list_virtual_keys() if k["key_hash"].startswith(key_hash)]
    if not matches:
        _output.error(f"No key matching hash '{key_hash}'")
    if len(matches) > 1:
        _output.error(f"Ambiguous hash '{key_hash}' matches {len(matches)} keys")
    kh = matches[0]["key_hash"]
    revoke_virtual_key(kh)
    write_audit_event("exa-gateway", _actor(), "virtual_key_revoked", kh, None)
    _output.ok(f"Revoked key {kh[:16]}…")


quota_app = typer.Typer(
    help="Per-tenant request quotas the serving gateway enforces (ADR 0123)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(quota_app, name="quota")


@quota_app.command(
    "set",
    epilog="Examples:\n\n  exa gateway quota set acme 120\n\n  exa gateway quota set batch 0  # unlimited",
)
def quota_set(
    tenant: str = typer.Argument(..., help="Tenant name (the virtual key's tenant)"),
    rpm: int = typer.Argument(
        ..., min=0, help="Requests per minute; 0 = unlimited for this tenant"
    ),
) -> None:
    """Cap a tenant's requests per minute. Reaches the gateway in the next serving snapshot."""
    from examlops.data import serving_quotas

    result = serving_quotas.set_quota(tenant, rpm, updated_by=_actor())
    if _output.json_mode:
        _output.print_json({"tenant": tenant, "rpm": rpm, "result": result})
        return
    _output.ok(f"{tenant}: {result}, {rpm or 'unlimited'} requests/min (via the serving snapshot)")


@quota_app.command("list")
def quota_list() -> None:
    """List the per-tenant quotas (tenants without one use EXAMLOPS_GATEWAY_TENANT_RPM)."""
    from examlops.data import serving_quotas

    rows = serving_quotas.list_quotas()
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No per-tenant quotas set; every tenant uses the gateway default.")
        return
    for row in rows:
        _output.info(f"{row['tenant']}  rpm={row['rpm']}  by={row['updated_by'] or '-'}")


@quota_app.command("remove")
def quota_remove(tenant: str = typer.Argument(..., help="Tenant whose quota to drop")) -> None:
    """Drop a tenant's quota so it falls back to the gateway default."""
    from examlops.data import serving_quotas

    removed = serving_quotas.remove_quota(tenant, updated_by=_actor())
    if _output.json_mode:
        _output.print_json({"tenant": tenant, "result": "removed" if removed else "not_found"})
        return
    if not removed:
        _output.error(f"No quota set for tenant '{tenant}'")
    _output.ok(f"{tenant}: quota removed (gateway default applies)")


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
    from examlops.data.gateway import cache_stats

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
    model: str = typer.Argument(
        "default", help="Route name: a registered LLM endpoint, or 'default' (echo)"
    ),
    message: str = typer.Option(..., "--message", help="User message"),
    key: str | None = typer.Option(None, "--key", help="Virtual key to authenticate with"),
    cache: bool = typer.Option(False, "--cache", help="Route through the B3 semantic cache"),
) -> None:
    """Send one chat message through the gateway, to a registered endpoint or the echo route."""
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
    from examlops.data.events import reasoning_usage_summary, structured_output_stats

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


@reasoning_app.command("set-budget")
def reasoning_set_budget(
    max_thinking: int = typer.Argument(..., min=0, help="Max thinking tokens per request"),
    model: str = typer.Option(None, "--model", help="Cap for this logical model"),
    project: str = typer.Option(None, "--project", help="Cap for this project's virtual keys"),
    key_hash: str = typer.Option(None, "--key-hash", help="Cap for one virtual key (its hash)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    remove: bool = typer.Option(False, "--remove", help="Delete the cap instead of setting it"),
) -> None:
    """Set (or --remove) a gateway reasoning budget; the tightest applicable cap wins (ADR 0035)."""
    from examlops.data import reasoning_budgets as store

    chosen = [(s, r) for s, r in (("model", model), ("project", project), ("key", key_hash)) if r]
    if len(chosen) != 1:
        _output.error("Pass exactly one of --model, --project, --key-hash.")
        raise typer.Exit(2)
    scope, ref = chosen[0]
    if remove:
        result = "removed" if store.remove(scope, ref, tenant) else "not_found"
    else:
        result = store.put(scope, ref, max_thinking, tenant)
    if _output.json_mode:
        _output.print_json(
            {"scope": scope, "ref": ref, "tenant": tenant, "max": max_thinking, "result": result}
        )
        return
    _output.ok(f"{scope} {ref} ({tenant}): {result}, max {max_thinking} thinking tokens")


@reasoning_app.command("budgets")
def reasoning_budgets(
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
    events: bool = typer.Option(False, "--events", help="Show recent budget outcomes instead"),
    outcome: str = typer.Option(
        None, "--outcome", help="With --events: within|exceeded|unknown|refused"
    ),
) -> None:
    """List configured reasoning budgets, or (--events) what the gateway observed against them."""
    from examlops.data import reasoning_budgets as store

    rows = store.list_events(outcome=outcome) if events else store.list_budgets(tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No reasoning budget events." if events else "No reasoning budgets set.")
        return
    for row in rows:
        _output.info("  ".join(f"{k}={v}" for k, v in row.items() if v is not None))
