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


@app.command("chat", epilog=_EXAMPLES)
def chat(
    model: str = typer.Argument("default", help="Logical model name to route"),
    message: str = typer.Option(..., "--message", help="User message"),
    key: str | None = typer.Option(None, "--key", help="Virtual key to authenticate with"),
) -> None:
    """Send one chat message through the gateway (uses the default echo route)."""
    from examlops.gateway import GatewayClient, GatewayError, build_default_router

    client = GatewayClient(build_default_router(), virtual_key=key)
    try:
        comp = client.chat(model, [{"role": "user", "content": message}])
    except GatewayError as exc:
        _output.error(f"{type(exc).__name__}: {exc}")
        return
    if _output.json_mode:
        _output.print_json({"text": comp.text, "backend": comp.backend, "cost_usd": comp.cost_usd})
        return
    _output.ok(f"[{comp.backend}] {comp.text}  (cost ${comp.cost_usd:.6f})")
