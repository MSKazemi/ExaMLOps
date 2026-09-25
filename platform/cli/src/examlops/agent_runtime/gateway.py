"""The runtime's one outbound route to tools (ADR 0145 decision 1): the tool gateway seam.

A node never calls a tool function. It asks the runtime, the runtime derives the idempotency key
and consults its journal, and only then does a :class:`ToolGateway` reach the tool. The default
gateway is the platform's tool broker (``examlops.tool_broker.invoke``, ADR 0145): grants,
tier ceilings, approval, egress checks, rate limits, credential injection and the evidence
chain - with the caller's grant set read from the agent snapshot rather than live.

The broker is in-process here. The ADR's open MCP gateway product (agentgateway / Envoy AI
Gateway) is a different :class:`ToolGateway` implementation behind this same seam; it is not
built.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from examlops.tool_broker.grants import GrantSet, ToolCaller

__all__ = ["BrokerGateway", "ToolGateway"]


@runtime_checkable
class ToolGateway(Protocol):
    def call(
        self,
        caller: ToolCaller,
        tool: str,
        args: dict[str, Any],
        *,
        approved: bool,
        idempotency_key: str,
        tenant: str,
    ) -> dict[str, Any]: ...


class BrokerGateway:
    """:class:`ToolGateway` over ``examlops.tool_broker.invoke`` in ``enforce`` mode."""

    def __init__(
        self,
        *,
        tools: Mapping[str, Any] | None = None,
        grant_resolver: Callable[[ToolCaller], GrantSet | None] | None = None,
        resolver: Any = None,
    ) -> None:
        self.tools = tools
        self.grant_resolver = grant_resolver
        self.resolver = resolver

    def call(
        self,
        caller: ToolCaller,
        tool: str,
        args: dict[str, Any],
        *,
        approved: bool,
        idempotency_key: str,
        tenant: str,
    ) -> dict[str, Any]:
        from examlops.tool_broker.broker import BrokerContext, invoke

        ctx = BrokerContext(
            mode="enforce",
            tenant=tenant,
            approved=approved,
            tools=self.tools,
            resolver=self.resolver,
            grant_resolver=self.grant_resolver,
        )
        spec = ctx.spec(tool)
        call_args = dict(args)
        # ADR 0147: a tool that takes an idempotency key gets the runtime's derived one, so a
        # crash between the tool acting and the runtime journaling its result still acts once.
        if spec is not None and "idempotency_key" in inspect.signature(spec.fn).parameters:
            call_args.setdefault("idempotency_key", idempotency_key)
        return invoke(caller, tool, call_args, ctx)
