"""Self-instrumentation of the live ReAct loop (Phase 2, ADR 0103).

Wires the *already-built* :mod:`examlops.agentops` telemetry into Skipper's reactive path so
that every chat turn records its tool calls into ``agent_sessions`` / ``agent_tool_calls`` —
making ``examlops.agentops.tool_success_rate`` real (today it is fed only by tests/autopilot).
On top of that it runs an in-loop :class:`~examlops.agentops.AgentCircuitBreaker` so a runaway
turn (repeating tool loop, step blow-up, all-error burst) is aborted early instead of burning
cycles.

Everything here is **best-effort and fail-open**: if ``examlops.agentops`` cannot be imported
(platform CLI not on the path) or ``platform.db`` is unavailable, :func:`start` returns a no-op
:class:`Instrumentation` and the chat turn proceeds exactly as before.
"""

from __future__ import annotations

import json
from typing import Any

from skipper import config

# ``examlops.agentops`` lives in the platform CLI package. The agent already puts
# ``platform/cli/src`` on ``sys.path`` (see ``skipper.tools.platform_ops``); import lazily and
# degrade to a no-op if it is not importable.
try:  # pragma: no cover - import guard
    from examlops.agentops import (
        AgentCircuitBreaker,
        AgentStep,
        CircuitBreakerTripped,
    )

    _AGENTOPS_OK = True
except Exception:  # noqa: BLE001
    _AGENTOPS_OK = False


def _tool_step(name: str, *, args: Any = None, ok: bool = True, error: str | None = None) -> Any:
    return AgentStep(tool=name or "tool", args=args, ok=ok, error=error)


class ToolCallArgs:
    """Remember each tool call's arguments so the loop breaker can tell calls apart.

    The breaker's rule is *same tool, same arguments, N times*, but a ``ToolMessage`` carries only
    a ``tool_call_id`` — the arguments were on the ``AIMessage`` that requested it. Without them
    every call to one tool collapses onto a single key, so an agent legitimately asking
    ``explain_command`` about three different commands is killed as a runaway loop on the third
    one, and the turn returns the breaker notice instead of an answer. Measured 2026-08-23: the
    30-question operator-QA set scored 0/30 this way, 13 answers empty and the rest breaker text.

    Streaming delivers the arguments as JSON fragments spread over chunks, so they are accumulated
    per call — by ``index`` (stable across a call's chunks) and mapped to the ``id`` the matching
    ``ToolMessage`` will quote.
    """

    def __init__(self) -> None:
        self._frags: dict[Any, list[str]] = {}
        self._id_of: dict[Any, Any] = {}
        self._by_id: dict[str, str] = {}

    def observe_ai(self, msg: Any) -> None:
        """Record the argument fragments (streaming) or full arguments (non-streaming)."""
        for chunk in getattr(msg, "tool_call_chunks", None) or []:
            index = chunk.get("index")
            key = index if index is not None else chunk.get("id")
            if key is None:
                continue
            if chunk.get("id"):
                self._id_of[key] = chunk["id"]
            self._frags.setdefault(key, []).append(chunk.get("args") or "")
            call_id = self._id_of.get(key)
            if call_id:
                self._by_id[call_id] = "".join(self._frags[key])
        for call in getattr(msg, "tool_calls", None) or []:
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if not call_id:
                continue
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
            if args:  # a chunk-accumulated value is already there in the streaming case
                self._by_id[call_id] = json.dumps(args, sort_keys=True, default=str)

    def args_for(self, tool_message: Any) -> str | None:
        """The arguments of the call this ``ToolMessage`` answers, if they were seen."""
        call_id = getattr(tool_message, "tool_call_id", None)
        return self._by_id.get(call_id) if call_id else None


class Instrumentation:
    """Per-turn telemetry sink: collect tool steps, guard the loop, flush on finish.

    A single instance spans one chat turn (one ``thread_id``/``session_id``). Callers feed it
    tool results with :meth:`observe`; :meth:`should_abort` reports whether the circuit-breaker
    tripped (so a streaming loop can stop); :meth:`finish` persists the session summary.
    """

    def __init__(self, session_id: str, *, model: str | None, agent: str = "skipper") -> None:
        self.session_id = session_id
        self._enabled = bool(config.AGENT_INSTRUMENT_ENABLED and _AGENTOPS_OK)
        self._recorder = None
        self._breaker = None
        self.tripped: Any | None = None
        self._unkeyed = 0
        if not self._enabled:
            return
        try:
            from examlops.agentops import SessionRecorder

            self._recorder = SessionRecorder(session_id, agent=agent, model=model)
            if config.AGENT_CIRCUIT_BREAKER:
                self._breaker = AgentCircuitBreaker(session_id=session_id)
        except Exception:  # noqa: BLE001 - never break a chat turn
            self._enabled = False

    def observe(
        self, tool_name: str, *, args: Any = None, ok: bool = True, error: str | None = None
    ) -> bool:
        """Record one tool result. Returns ``True`` if the loop should abort (breaker tripped).

        *args* are what distinguishes one call from a repeat of the same call; pass them whenever
        they are known (see :class:`ToolCallArgs`) or the loop rule degrades into "this tool was
        used N times".
        """
        if not self._enabled:
            return False
        if args is None:
            # The loop rule means "this exact call, again". When the arguments are not observable —
            # LangGraph's ``messages`` stream surfaces a subgraph's ``ToolMessage`` without the
            # ``AIMessage`` that requested it, so nothing on that path carries them — an empty key
            # makes every call to one tool identical and kills the third one. A unique marker keeps
            # such a step out of the loop rule; runaway turns stay bounded by the step-blowup cap
            # and the error-burst rule, neither of which needs arguments.
            self._unkeyed += 1
            args = f"unobserved-call-{self._unkeyed}"
        step = _tool_step(tool_name, args=args, ok=ok, error=error)
        try:
            if self._recorder is not None:
                self._recorder.add(step)
            if self._breaker is not None:
                self._breaker.guard(step)
        except CircuitBreakerTripped as trip:  # runaway loop / step blow-up / error burst
            self.tripped = trip.anomaly
            return True
        except Exception:  # noqa: BLE001 - telemetry must never crash the turn
            self._enabled = False
        return False

    @property
    def abort_message(self) -> str | None:
        if self.tripped is None:
            return None
        return f"agent stopped by circuit-breaker: {self.tripped.code} — {self.tripped.detail}"

    def finish(self) -> list[Any]:
        """Persist the session summary + return detected anomalies (best-effort)."""
        if not self._enabled or self._recorder is None:
            return []
        try:
            return self._recorder.flush()
        except Exception:  # noqa: BLE001
            return []


def start(session_id: str, *, model: str | None = None) -> Instrumentation:
    """Create an :class:`Instrumentation` for one turn (a no-op sink when disabled/unavailable)."""
    return Instrumentation(session_id, model=model or config.AGENT_MODEL)


def tool_status(msg: Any) -> tuple[bool, str | None]:
    """Extract ``(ok, error)`` from a LangChain ``ToolMessage`` (defensive across versions)."""
    status = getattr(msg, "status", None)
    if status == "error":
        content = getattr(msg, "content", "") or ""
        return False, (content if isinstance(content, str) else str(content))[:500]
    return True, None
