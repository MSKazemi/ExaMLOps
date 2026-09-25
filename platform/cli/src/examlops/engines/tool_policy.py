"""Tool-call validity is enforced by policy (ADR 0143 decision 9).

``tool_choice="auto"`` lets a model answer in prose *or* call a tool, and vLLM's own documentation
says auto mode does not guarantee parseable arguments. For an **agent tool step** — a request the
caller marks with ``tool_step=True`` — the policy therefore upgrades the request to
``tool_choice="required"`` (or keeps a caller's *named* function choice, which is already
constrained), so the server's guided decoding produces arguments that parse.

Parse failures are a tracked metric (:func:`parse_stats`, and ``examlops_tool_call_parse_total``
through :func:`prometheus_lines`): the policy is only as good as its measured failure rate.

``EXAMLOPS_TOOL_CHOICE_POLICY``: ``enforce`` (default) | ``off``. ``off`` passes the caller's
``tool_choice`` through untouched and still counts failures.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

__all__ = [
    "apply_tool_choice_policy",
    "parse_stats",
    "policy_mode",
    "prometheus_lines",
    "record_tool_calls",
    "reset_parse_stats",
    "validate_tool_calls",
]

_LOCK = threading.Lock()
_STATS: dict[str, int] = {"ok": 0, "parse_error": 0, "missing": 0}
_MAX_TOOLS = 128  # bounded: a request carrying more tool definitions than this is refused


def policy_mode() -> str:
    mode = os.getenv("EXAMLOPS_TOOL_CHOICE_POLICY", "enforce").strip().lower()
    return mode if mode in ("enforce", "off") else "enforce"  # fail closed on a typo


def _named_choice(choice: Any) -> bool:
    return (
        isinstance(choice, dict)
        and choice.get("type") == "function"
        and isinstance(choice.get("function"), dict)
        and bool(choice["function"].get("name"))
    )


def apply_tool_choice_policy(
    body: dict[str, Any],
    *,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
    tool_step: bool = False,
    mode: str | None = None,
) -> dict[str, Any]:
    """Put ``tools`` / ``tool_choice`` on ``body`` according to the policy; returns ``body``."""
    if not tools:
        if tool_choice not in (None, "none"):
            raise ValueError("tool_choice was given without any tools")
        return body
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise ValueError(f"tools must be a list of at most {_MAX_TOOLS} definitions")
    body["tools"] = tools
    chosen = mode or policy_mode()
    if tool_step and chosen == "enforce":
        if _named_choice(tool_choice) or tool_choice == "required":
            body["tool_choice"] = tool_choice
        else:
            # `auto`, `none` or absent on a tool step all become `required`: the agent asked for a
            # tool call, and only a constrained choice guarantees parseable arguments.
            body["tool_choice"] = "required"
    elif tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def validate_tool_calls(tool_calls: Any) -> tuple[bool, list[str]]:
    """``(ok, errors)`` — every call names a function and its arguments are a JSON object."""
    if not tool_calls:
        return False, ["no tool call in the response"]
    errors: list[str] = []
    for i, call in enumerate(tool_calls if isinstance(tool_calls, list) else [tool_calls]):
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            errors.append(f"tool_calls[{i}] names no function")
            continue
        args = fn.get("arguments", "{}")
        if isinstance(args, dict):
            continue
        try:
            parsed = json.loads(args) if isinstance(args, str) else None
        except (TypeError, ValueError) as exc:
            errors.append(f"tool_calls[{i}] arguments do not parse: {exc}")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"tool_calls[{i}] arguments are not a JSON object")
    return not errors, errors


def record_tool_calls(tool_calls: Any) -> tuple[bool, list[str]]:
    """Validate and count one tool-step response (``missing`` when it called no tool)."""
    ok, errors = validate_tool_calls(tool_calls)
    key = "ok" if ok else ("missing" if not tool_calls else "parse_error")
    with _LOCK:
        _STATS[key] += 1
    return ok, errors


def parse_stats() -> dict[str, Any]:
    with _LOCK:
        snap = dict(_STATS)
    total = sum(snap.values())
    return {
        **snap,
        "total": total,
        "failure_rate": ((snap["parse_error"] + snap["missing"]) / total) if total else None,
    }


def reset_parse_stats() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0


def prometheus_lines() -> list[str]:
    """``examlops_tool_call_parse_total{outcome=...}`` in exposition format."""
    snap = parse_stats()
    lines = [
        "# HELP examlops_tool_call_parse_total Agent tool-step responses by parse outcome",
        "# TYPE examlops_tool_call_parse_total counter",
    ]
    for outcome in ("ok", "parse_error", "missing"):
        lines.append(f'examlops_tool_call_parse_total{{outcome="{outcome}"}} {snap[outcome]}')
    return lines
