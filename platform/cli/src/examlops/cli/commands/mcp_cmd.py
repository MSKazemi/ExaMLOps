"""``exa mcp`` — expose ExaMLOps to LLM agents and MCP clients (agent-to-agent surface)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import typer

from examlops.cli import _output


class TransportEnum(StrEnum):
    stdio = "stdio"
    http = "http"


_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# List every tool an agent can call[/dim]\n"
    "  exa mcp tools\n\n"
    "  [dim]# Serve over stdio for Claude Desktop / Claude Code / any MCP client[/dim]\n"
    "  exa mcp serve\n\n"
    "  [dim]# Serve over HTTP, allowing mutating tools (retrain)[/dim]\n"
    "  exa mcp serve --transport http --port 8765 --allow-writes\n\n"
    "  [dim]# Print the A2A Agent Card for agent-to-agent discovery[/dim]\n"
    "  exa mcp agent-card --json\n\n"
    "  [dim]# OAuth scopes each tool needs on the authenticated HTTP transport[/dim]\n"
    "  exa mcp scopes"
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Model Context Protocol server + Agent-to-Agent (A2A) surface.",
    epilog=_EXAMPLES,
)


def _hints(spec: Any) -> str:
    """Compact MCP safety hints: ro / destructive / idempotent / open-world."""
    a = spec.annotations
    out = ["ro"] if a["readOnlyHint"] else []
    out += ["destructive"] if a.get("destructiveHint") else []
    out += ["idempotent"] if a.get("idempotentHint") else []
    out += ["open-world"] if a["openWorldHint"] else []
    return ", ".join(out) or "—"


@app.command("tools", epilog=_EXAMPLES)
def tools(
    show_writes: bool = typer.Option(
        False, "--all", help="Include mutating (write) tools even if writes are disabled"
    ),
) -> None:
    """List the tools ExaMLOps exposes to agents over MCP."""
    from examlops.mcp.tools import iter_tools

    specs = list(iter_tools(include_writes=True if show_writes else None))
    rows = [
        [
            spec.name,
            "write" if spec.mutating else "read",
            _hints(spec),
            ", ".join(spec.tags) or "—",
            spec.description,
        ]
        for spec in specs
    ]
    _output.print_table(
        "Agent-callable tools (MCP)", ["Tool", "Kind", "Hints", "Tags", "Description"], rows
    )
    if not any(s.mutating for s in specs):
        _output.hint(
            "Mutating tools are hidden. Show them with --all, or enable them for the "
            "server with --allow-writes / EXAMLOPS_MCP_ALLOW_WRITES=1."
        )


@app.command("capabilities", epilog=_EXAMPLES)
def capabilities(
    show_writes: bool = typer.Option(
        False, "--all", help="Include mutating (write) tools even if writes are disabled"
    ),
) -> None:
    """Show what the agent can do, grouped by lifecycle use case (management, monitoring, …)."""
    from examlops.mcp.tools import capabilities_catalogue

    catalogue = capabilities_catalogue(include_writes=True if show_writes else None)
    if _output.json_mode:
        _output.print_json(catalogue)
        return
    for use_case, tools in catalogue.items():
        rows = [
            [
                t["name"],
                t["tier"] if t["mutating"] else "read",
                t["description"],
            ]
            for t in tools
        ]
        _output.print_table(
            f"{use_case.title()} — {len(tools)} capabilities",
            ["Tool", "Tier", "Description"],
            rows,
        )
    _output.hint(
        "Write tiers: A=autopilot-OK · B=confirm-required · C=human-only. --all shows writes."
    )


@app.command("resources", epilog=_EXAMPLES)
def resources() -> None:
    """List the MCP resources (readable context) ExaMLOps exposes to agents."""
    from examlops.mcp.resources import iter_resources

    rows = [
        [r.uri, r.name, "template" if r.templated else "static", r.description]
        for r in iter_resources()
    ]
    _output.print_table("MCP resources", ["URI", "Name", "Kind", "Description"], rows)


@app.command("prompts", epilog=_EXAMPLES)
def prompts() -> None:
    """List the MCP prompts (reusable agent workflows) ExaMLOps ships."""
    from examlops.mcp.prompts import iter_prompts

    rows = [[p.name, ", ".join(p.tags) or "—", p.description] for p in iter_prompts()]
    _output.print_table("MCP prompts", ["Name", "Tags", "Description"], rows)


@app.command("agent-card", epilog=_EXAMPLES)
def agent_card(
    base_url: str = typer.Option("", "--url", help="Public base URL where this agent is reachable"),
    all_tools: bool = typer.Option(False, "--all", help="Advertise mutating tools in the card"),
) -> None:
    """Print the A2A Agent Card describing this platform's agent skills."""
    from examlops.mcp.agent_card import build_agent_card

    card = build_agent_card(
        base_url=base_url or None,
        include_writes=True if all_tools else None,
    )
    if _output.json_mode:
        _output.print_json(card)
        return
    _output.print_record(
        {
            "name": card["name"],
            "version": card["version"],
            "protocol": card["protocolVersion"],
            "skills": len(card["skills"]),
            "transports": ", ".join(card["interfaces"]["mcp"]["transports"]),
        }
    )
    _output.hint("Full machine-readable card: exa --json mcp agent-card")


@app.command("scopes", epilog=_EXAMPLES)
def scopes(
    show_writes: bool = typer.Option(
        True, "--all/--reads", help="Include mutating (write) tools (default) or reads only"
    ),
) -> None:
    """Show the OAuth scope each MCP tool needs on the authenticated HTTP transport (ADR 0082)."""
    from examlops.mcp.http_auth import scope_catalog

    catalog = scope_catalog(include_writes=show_writes)
    if _output.json_mode:
        _output.print_json(catalog)
        return
    _output.print_table(
        "MCP OAuth scopes",
        ["Scope", "Grants"],
        [[name, grants] for name, grants in catalog["coarse"].items()],
    )
    _output.print_table(
        "Scopes accepted per tool (any one suffices)",
        ["Tool", "Tier", "Scopes"],
        [[name, t["tier"], " ".join(t["scopes"])] for name, t in catalog["tools"].items()],
    )
    _output.hint(
        "Enforced when `exa mcp serve --transport http` runs with EXAMLOPS_MCP_AUTH=oauth; "
        f"per-tool scopes are {catalog['per_tool_prefix']}<tool>."
    )


@app.command("serve", epilog=_EXAMPLES)
def serve(
    transport: TransportEnum = typer.Option(  # type: ignore[valid-type]
        TransportEnum.stdio, "--transport", help="Transport for the MCP server"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (http transport)"),
    port: int = typer.Option(8765, "--port", help="Bind port (http transport)"),
    allow_writes: bool = typer.Option(
        False, "--allow-writes", help="Register mutating tools (retrain). Off by default."
    ),
) -> None:
    """Run the MCP server so agents can drive ExaMLOps."""
    from examlops.mcp.server import FastMCPNotInstalled, McpAuthConfigError, UnsafeMCPBind
    from examlops.mcp.server import serve as _serve

    transport_value = transport.value if hasattr(transport, "value") else str(transport)
    include_writes = True if allow_writes else None
    try:
        if transport_value == "stdio":
            # stdio must keep stdout clean for the protocol — announce on stderr only.
            _output.err_console.print(
                "[dim]ExaMLOps MCP server on stdio "
                f"(writes {'enabled' if allow_writes else 'disabled'})[/dim]"
            )
        else:
            import os

            auth = (os.getenv("EXAMLOPS_MCP_AUTH", "") or "none").strip().lower()
            _output.info(
                f"ExaMLOps MCP server on http://{host}:{port} "
                f"(writes {'enabled' if allow_writes else 'disabled'}, auth {auth})"
            )
        _serve(
            transport=transport_value,
            host=host,
            port=port,
            include_writes=include_writes,
        )
    except (FastMCPNotInstalled, UnsafeMCPBind, McpAuthConfigError) as exc:
        _output.error(str(exc))
    except KeyboardInterrupt:  # pragma: no cover
        _output.info("MCP server stopped.")
