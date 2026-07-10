"""``exa providers`` — cross-domain discovery for the pluggable-calculation substrate (ADR 0077).

One place to see every extension point of the platform: which calculation providers are installed for
each domain (carbon, cost, placement, drift, promotion, …), where each came from (builtin / entry-point
plugin / config), and whether it loaded. This makes the "programmable" surface *discoverable* — the
umbrella invariant (ADR 0076): a broken plugin is shown with its error, never silently dropped.
"""

from __future__ import annotations

import importlib

import typer

from .. import _output

app = typer.Typer(no_args_is_help=True, help="Pluggable calculation providers (all domains)")

# domain -> module whose import registers that domain's built-ins (side-effect registration).
_DOMAIN_MODULES: dict[str, str] = {
    "carbon": "examlops.finops.carbon_providers",
    "cost": "examlops.finops.cost_providers",
    "placement": "examlops.hpc_placement_providers",
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
