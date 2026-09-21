"""FastMCP server exposing the ExaMLOps tool registry to MCP clients and agents.

FastMCP is an **optional** dependency (``examlops[mcp]``). It is imported lazily so the
core CLI and the pure :mod:`examlops.mcp.tools` registry work without it. Read-only tools
are always registered; mutating tools are only registered when writes are enabled.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from examlops.mcp.prompts import iter_prompts
from examlops.mcp.resources import iter_resources
from examlops.mcp.tools import iter_tools


class FastMCPNotInstalled(RuntimeError):
    """Raised when FastMCP is required but not installed."""

    def __init__(self) -> None:
        super().__init__(
            "FastMCP is not installed. Install the MCP extra:\n"
            "    uv pip install 'examlops[mcp]'\n"
            "or:\n"
            "    pip install fastmcp"
        )


class UnsafeMCPBind(RuntimeError):
    """Raised when the unauthenticated HTTP transport would be exposed remotely."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _import_fastmcp() -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise FastMCPNotInstalled() from exc
    return FastMCP


def build_server(*, name: str = "ExaMLOps", include_writes: bool | None = None) -> Any:
    """Build and return a configured FastMCP server (not yet running).

    Args:
        name: Server name advertised to MCP clients.
        include_writes: Register mutating tools. ``None`` => env-driven
            (``EXAMLOPS_MCP_ALLOW_WRITES``).
    """
    fast_mcp = _import_fastmcp()
    server = fast_mcp(name)
    broker = _broker_setup()
    for spec in iter_tools(include_writes=include_writes):
        if broker is not None:
            if not _broker_visible(broker, spec):
                continue  # ADR 0145 d2: tools/list shows only what the caller may call
            spec = _brokered_spec(broker, spec)
        # FastMCP derives the tool schema from the function's type hints + docstring.
        # Safety annotations (readOnly/destructive/idempotent/openWorld hints) let a client
        # decide what to auto-approve. `annotations=` needs FastMCP >= 2.2.7; an older build
        # rejects the keyword, so degrade to an unannotated tool rather than fail to serve.
        try:
            server.tool(name=spec.name, description=spec.description, annotations=spec.annotations)(
                spec.fn
            )
        except TypeError:  # pragma: no cover - only on a FastMCP that predates annotations
            server.tool(name=spec.name, description=spec.description)(spec.fn)
    for res in iter_resources():
        server.resource(
            res.uri, name=res.name, description=res.description, mime_type=res.mime_type
        )(res.fn)
    for prompt in iter_prompts():
        server.prompt(name=prompt.name, description=prompt.description)(prompt.fn)
    return server


def _broker_setup() -> tuple[str, Any] | None:
    """``(mode, caller)`` when ``EXAMLOPS_TOOL_BROKER`` is monitor/enforce, else ``None`` (off)."""
    from examlops.tool_broker import ToolCaller, broker_mode, caller_from_env

    mode = broker_mode()
    if mode == "off":
        return None
    return mode, caller_from_env() or ToolCaller(agent="anonymous")


def _broker_visible(broker: tuple[str, Any], spec: Any) -> bool:
    """Enforce mode hides tools the caller cannot call; monitor mode hides nothing."""
    mode, caller = broker
    if mode != "enforce":
        return True
    from examlops.tool_broker import resolve_grant_set, tool_visible

    try:
        return tool_visible(resolve_grant_set(caller), spec.name, spec.tier)
    except Exception:  # noqa: BLE001 - cannot read grants: list nothing the call path would refuse
        return False


def _brokered_spec(broker: tuple[str, Any], spec: Any) -> Any:
    """``spec`` with its function routed through :func:`examlops.tool_broker.invoke`."""
    import functools
    import inspect
    from dataclasses import replace

    from examlops.tool_broker import BrokerContext, invoke

    mode, caller = broker
    fn = spec.fn
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def brokered(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = sig.bind_partial(*args, **kwargs)
        return invoke(caller, spec.name, dict(bound.arguments), BrokerContext(mode=mode))

    return replace(spec, fn=brokered)


def serve(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    include_writes: bool | None = None,
) -> None:
    """Run the MCP server (blocking).

    Args:
        transport: ``stdio`` (for desktop/CLI agent clients) or ``http`` (network).
        host: Bind host for the ``http`` transport.
        port: Bind port for the ``http`` transport.
        include_writes: Register mutating tools. ``None`` => env-driven.
    """
    if transport == "stdio":
        server = build_server(include_writes=include_writes)
        server.run(transport="stdio")
    else:
        if not _is_loopback(host):
            raise UnsafeMCPBind(
                "MCP HTTP has no built-in authentication and may bind only to loopback. "
                "Keep --host 127.0.0.1 and place an authenticated TLS reverse proxy in front "
                "for remote access."
            )
        server = build_server(include_writes=include_writes)
        server.run(transport="http", host=host, port=port)
