"""``exa providers`` — cross-domain discovery for the pluggable-calculation substrate (ADR 0077).

One place to see every extension point of the platform: which calculation providers are installed for
each domain (carbon, cost, placement, drift, promotion, …), where each came from (builtin / entry-point
plugin / config), and whether it loaded. This makes the "programmable" surface *discoverable* — the
umbrella invariant (ADR 0076): a broken plugin is shown with its error, never silently dropped.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import typer

from .. import _output

app = typer.Typer(no_args_is_help=True, help="Pluggable calculation providers (all domains)")

# domain -> module whose import registers that domain's built-ins (side-effect registration).
_DOMAIN_MODULES: dict[str, str] = {
    "carbon": "examlops.finops.carbon_providers",
    "cost": "examlops.finops.cost_providers",
    "drift": "examlops.drift_providers",
    "llm_cache": "examlops.llmops_providers",
    "llm_cost": "examlops.llmops_providers",
    "llm_routing": "examlops.llmops_providers",
    "placement": "examlops.hpc_placement_providers",
    "policy": "examlops.policy_engine.providers",
    "promotion": "examlops.promotion_providers",
    "rag_quality": "examlops.llmops_providers",
}

_EXAMPLES = (
    "Examples:\n\n"
    "  exa providers list\n\n"
    "  exa providers list --domain placement\n\n"
    "  exa providers list --json\n\n"
    "Add your own: ship a plugin under the 'exa.providers.<domain>' entry-point group, or write a\n"
    "formula in ~/.config/examlops/providers.yaml. See docs/guides/programmable-mlops.md."
)


def _register(domain: str) -> None:
    module = _DOMAIN_MODULES.get(domain)
    if module:
        importlib.import_module(module)  # registers built-ins as a side effect


@app.command(epilog=_EXAMPLES)
def list(  # noqa: A001 - CLI verb; shadowing builtin is intentional and local
    domain: str | None = typer.Option(
        None, "--domain", "-d", help="Only this domain (default: all known domains)"
    ),
):
    """List calculation providers across every domain (built-ins + entry-point plugins + config)."""
    from examlops.providers import default_provider_name, list_providers

    domains = [domain] if domain else sorted(_DOMAIN_MODULES)
    for d in domains:
        _register(d)

    if _output.json_mode:
        payload = []
        for d in domains:
            default = default_provider_name(d)
            for i in list_providers(d):
                payload.append(
                    {
                        "domain": d,
                        "name": i.name,
                        "kind": i.kind,
                        "default": i.name == default,
                        "ok": i.ok,
                        "error": i.error,
                    }
                )
        _output.print_json(payload)
        return

    for d in domains:
        default = default_provider_name(d)
        rows = []
        for i in list_providers(d):
            meta = i.provider.metadata() if i.ok and i.provider else None
            method = (meta.methodology if meta else "") or "—"
            status = "ok" if i.ok else f"ERROR: {i.error}"
            rows.append(
                [
                    i.name + ("  (default)" if i.name == default else ""),
                    i.kind,
                    method,
                    status,
                ]
            )
        _output.print_table(f"providers · {d}", ["Name", "Kind", "Methodology", "Status"], rows)


# ── authored providers (notebook/dashboard, per-project, AST-sandboxed) ──────────

_AUTHOR_EXAMPLES = (
    "Examples:\n\n"
    "  exa providers author cost my-cost --project research --file my_cost.py\n\n"
    "  exa providers authored --project research\n\n"
    "  exa providers show cost my-cost --project research\n\n"
    "  exa providers rm cost my-cost --project research\n\n"
    "Author from a notebook instead:\n"
    "  from examlops.providers import register_from_source, save_provider\n"
    "See docs/guides/authored-providers.md."
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command(epilog=_AUTHOR_EXAMPLES)
def author(
    domain: str = typer.Argument(..., help="Provider domain (cost, carbon, drift, …)"),
    name: str = typer.Argument(..., help="Provider name (referenced via --provider)"),
    file: Path = typer.Option(..., "--file", "-f", help="Python file defining a Provider subclass"),
    project: str = typer.Option(..., "--project", "-p", help="Project that owns the provider"),
):
    """Save a project-scoped provider from a Python file (AST-sandboxed; audited)."""
    from examlops.data.audit import write_audit_event
    from examlops.providers import ProviderError, save_provider

    if not file.exists():
        _output.error(f"file not found: {file}")
        raise typer.Exit(1)
    code = file.read_text(encoding="utf-8")
    try:
        info = save_provider(domain, name, code, project=project, actor=_actor())
    except ProviderError as exc:
        _output.error(f"rejected: {exc}")
        raise typer.Exit(1) from exc
    write_audit_event(
        "cli", _actor(), "provider_authored", f"{project}/{domain}/{name}", {"class": info["class"]}
    )
    _output.ok(
        f"Saved provider '{name}' (domain {domain}) for project '{project}' → {info['path']}"
    )


@app.command(epilog=_AUTHOR_EXAMPLES)
def authored(
    project: str = typer.Option(
        ..., "--project", "-p", help="Project to list authored providers for"
    ),
):
    """List a project's notebook/dashboard-authored providers (with gate status)."""
    from examlops.providers import list_project_providers

    rows = list_project_providers(project)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info(f"No authored providers for project '{project}'.")
        return
    _output.print_table(
        f"authored providers · {project}",
        ["Domain", "Name", "Status"],
        [[r["domain"], r["name"], "ok" if r["ok"] else f"ERROR: {r['error']}"] for r in rows],
    )


@app.command(epilog=_AUTHOR_EXAMPLES)
def show(
    domain: str = typer.Argument(...),
    name: str = typer.Argument(...),
    project: str = typer.Option(..., "--project", "-p"),
):
    """Print the stored source of an authored provider."""
    from examlops.providers import ProviderError, read_provider_source

    try:
        typer.echo(read_provider_source(project, domain, name))
    except ProviderError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc


@app.command(epilog=_AUTHOR_EXAMPLES)
def rm(
    domain: str = typer.Argument(...),
    name: str = typer.Argument(...),
    project: str = typer.Option(..., "--project", "-p"),
):
    """Delete an authored provider file (audited)."""
    from examlops.data.audit import write_audit_event
    from examlops.providers import delete_provider

    if delete_provider(project, domain, name):
        write_audit_event("cli", _actor(), "provider_removed", f"{project}/{domain}/{name}", {})
        _output.ok(f"Removed provider '{name}' (domain {domain}) from project '{project}'.")
    else:
        _output.warning(f"No such provider '{name}' (domain {domain}) in project '{project}'.")


@app.command(epilog=_AUTHOR_EXAMPLES)
def validate(
    file: Path = typer.Option(
        ..., "--file", "-f", help="Python file to gate-check (no side effects)"
    ),
):
    """Statically validate a provider file against the AST sandbox (exit 1 if rejected). CI-safe."""
    from examlops.providers import ProviderError, compile_provider

    if not file.exists():
        _output.error(f"file not found: {file}")
        raise typer.Exit(1)
    try:
        cls = compile_provider(file.read_text(encoding="utf-8"))
    except ProviderError as exc:
        _output.error(f"rejected: {exc}")
        raise typer.Exit(1) from exc
    _output.ok(f"OK — defines Provider subclass '{cls.__name__}'.")


@app.command(epilog=_AUTHOR_EXAMPLES)
def activate(
    domain: str = typer.Argument(...),
    name: str = typer.Argument(...),
    project: str = typer.Option(..., "--project", "-p"),
):
    """Make a provider the active one for its (project, domain) — used when no --provider is given."""
    from examlops.data.audit import write_audit_event
    from examlops.providers import ProviderError, set_active_provider

    try:
        set_active_provider(project, domain, name)
    except ProviderError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    write_audit_event("cli", _actor(), "provider_activated", f"{project}/{domain}/{name}", {})
    _output.ok(f"'{name}' is now the active {domain} provider for project '{project}'.")
