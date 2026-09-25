"""W3C trace-context propagation across the platform's outbound hops (ADR 0148 decision 1).

"One trace from agent to tensor" needs every hop to carry ``traceparent`` (and ``tracestate``):
OpenAI-compatible calls and OIP v2 over HTTP headers, and MCP tool calls over
``params._meta`` (the MCP convention for request metadata). This module is the one place that
does it, so no call site hand-formats a header.

Source of the context, in order:

1. the active OpenTelemetry span, via the SDK's global propagator, when OpenTelemetry is installed
   and enabled (``OTEL_SDK_DISABLED`` falsy);
2. otherwise an inbound context the caller bound with :func:`bind_inbound` (a service that
   received a ``traceparent`` and makes a downstream call while tracing is off still forwards it,
   so the trace is not cut at the first untraced hop).

A malformed inbound ``traceparent`` is dropped, never forwarded: propagating garbage would attach
spans to a trace that does not exist. Nothing here ever raises into the caller.
"""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

__all__ = [
    "bind_inbound",
    "current_context",
    "extract",
    "inject_http_headers",
    "inject_mcp_meta",
    "valid_traceparent",
]

# version "00" format: 00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>; all-zero ids invalid.
_TP = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_MAX_TRACESTATE = 512  # W3C allows 32 list members; bound what we forward
_INBOUND: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "examlops_inbound_trace", default=None
)
_TRUTHY = {"1", "true", "yes", "on"}


def valid_traceparent(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    m = _TP.match(value.strip().lower())
    if not m:
        return False
    version, trace_id, parent_id, _ = m.groups()
    return version != "ff" and set(trace_id) != {"0"} and set(parent_id) != {"0"}


def extract(carrier: Mapping[str, Any]) -> dict[str, str]:
    """The valid trace-context fields of an inbound header map or MCP ``_meta`` (case-insensitive)."""
    lowered = {str(k).lower(): v for k, v in (carrier or {}).items()}
    tp = lowered.get("traceparent")
    if not valid_traceparent(tp):
        return {}
    out = {"traceparent": str(tp).strip().lower()}
    ts = lowered.get("tracestate")
    if (
        isinstance(ts, str)
        and ts
        and len(ts) <= _MAX_TRACESTATE
        # Re-emitted verbatim as an outbound header value: printable ASCII only, so an inbound
        # CR/LF (or any control byte) can never split or smuggle a header downstream.
        and all(" " <= c <= "~" for c in ts)
    ):
        out["tracestate"] = ts.strip()
    return out


@contextmanager
def bind_inbound(carrier: Mapping[str, Any]) -> Iterator[dict[str, str]]:
    """Bind an inbound request's trace context for the duration of the handler."""
    ctx = extract(carrier)
    token = _INBOUND.set(ctx or None)
    try:
        yield ctx
    finally:
        _INBOUND.reset(token)


def _otel_enabled() -> bool:
    return os.getenv("OTEL_SDK_DISABLED", "true").strip().lower() not in _TRUTHY


def _from_otel() -> dict[str, str]:
    if not _otel_enabled():
        return {}
    try:
        from opentelemetry import propagate
    except ImportError:
        return {}
    carrier: dict[str, str] = {}
    try:
        propagate.inject(carrier)
    except Exception:  # noqa: BLE001 - a propagator fault must not break the call it decorates
        return {}
    return extract(carrier)


def current_context() -> dict[str, str]:
    """``{"traceparent": ..., ["tracestate": ...]}`` for the current hop, or ``{}``."""
    return _from_otel() or dict(_INBOUND.get() or {})


def inject_http_headers(headers: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Add ``traceparent``/``tracestate`` to outbound HTTP headers (OpenAI-compatible, OIP v2).

    A caller-supplied valid ``traceparent`` is kept; nothing is added when there is no context.
    """
    try:
        if valid_traceparent(headers.get("traceparent")):
            return headers
        headers.update(current_context())
    except Exception:  # noqa: BLE001
        pass
    return headers


def inject_mcp_meta(params: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Add trace context to an MCP request's ``params._meta`` (creating ``_meta`` when absent)."""
    try:
        ctx = current_context()
        if not ctx:
            return params
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        if not valid_traceparent(meta.get("traceparent")):
            meta.update(ctx)
        params["_meta"] = meta
    except Exception:  # noqa: BLE001
        pass
    return params
