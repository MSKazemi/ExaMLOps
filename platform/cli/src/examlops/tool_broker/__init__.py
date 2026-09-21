"""The agent tool broker (ADR 0145, tool half): grants, a pure decision, an enforcing wrapper.

Behaviour-preserving by construction: a caller with no grant set is not brokered, and the MCP
server consults the broker only when ``EXAMLOPS_TOOL_BROKER`` is ``monitor`` or ``enforce``.
"""

from examlops.tool_broker.broker import (
    BrokerContext,
    broker_mode,
    caller_from_env,
    invoke,
    redact_args,
    simulate,
)
from examlops.tool_broker.grants import (
    GRANT_SCHEMA_VERSION,
    GrantError,
    GrantSet,
    ToolCall,
    ToolCaller,
    ToolDecision,
    ToolGrant,
    decide_tool_call,
    parse_grant,
    tool_visible,
)
from examlops.tool_broker.service import (
    list_grants,
    load_grant_set,
    remove_grant,
    resolve_grant_set,
    set_grant,
)

__all__ = [
    "GRANT_SCHEMA_VERSION",
    "BrokerContext",
    "GrantError",
    "GrantSet",
    "ToolCall",
    "ToolCaller",
    "ToolDecision",
    "ToolGrant",
    "broker_mode",
    "caller_from_env",
    "decide_tool_call",
    "invoke",
    "list_grants",
    "load_grant_set",
    "parse_grant",
    "redact_args",
    "remove_grant",
    "resolve_grant_set",
    "set_grant",
    "simulate",
    "tool_visible",
]
