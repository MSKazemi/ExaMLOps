"""Next-Gen 40 · C4 — AgentOps: agent trace & tool-call analytics (ADR 0021).

Turns C1 AGENT/TOOL spans into per-session analytics: tool success rate, step
counts, reasoning-loop / blowup / cost-overrun detection, and dashboard replay —
tenant-scoped (D6) with PII-redacted tool args (D8) and dangerous-tool audit (D4).

Graceful degradation: works with only ``platform_db`` (pure SQLite). If the D8
guardrails module is importable, tool args are redacted before hashing; otherwise
a plain hash is used (args are never stored raw regardless). If C1 GenAI telemetry
is available, ``record_session`` also emits spans; if not, it silently skips them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

# Default detection thresholds — overridable per call. Kept conservative so a
# normal Skipper session (a handful of tool calls) never trips them.
LOOP_REPEAT_THRESHOLD = 3  # same (tool, args) seen >= N times => loop
STEP_BLOWUP_THRESHOLD = 40  # more than N steps => runaway reasoning
COST_OVERRUN_DEFAULT = 1.0  # USD per session before an overrun anomaly
# Warn-before-abort (ADR 0021 decision 4): the fraction of each hard threshold at which the
# in-loop breaker starts *warning*. 0 disables the soft threshold; the hard abort is unaffected.
WARN_RATIO_DEFAULT = 0.75
WARN_RATIO_ENV = "EXAMLOPS_AGENT_BREAKER_WARN_RATIO"


@dataclass(frozen=True)
class AgentStep:
    """One tool invocation inside a session (R1)."""

    tool: str
    args: dict[str, Any] | str | None = None
    ok: bool = True
    error: str | None = None
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    step: int | None = None  # explicit index; falls back to position


@dataclass(frozen=True)
class Anomaly:
    """A detected problem in a session (R4)."""

    code: str  # loop | step_blowup | cost_overrun | error_burst
    severity: str  # warn | critical
    detail: str
    tool: str | None = None


# Dangerous tools whose use is always audited to D4 (audit_events).
_DANGEROUS_TOOLS = {
    "trigger_retrain",
    "promote_model",
    "approve_cluster",
    "delete",
    "shell",
    "exec",
}


def _redact(args: dict[str, Any] | str | None) -> str:
    """Redact PII from tool args (D8) then hash — raw args are never stored."""
    if args is None:
        return ""
    text = args if isinstance(args, str) else json.dumps(args, sort_keys=True, default=str)
    try:  # D8 guardrails is optional — degrade to the raw text if absent.
        from examlops.guardrails import redact_pii

        text, _found = redact_pii(text)
    except Exception:
        pass
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def record_session(
    session_id: str,
    tenant: str,
    steps: Sequence[AgentStep],
    *,
    agent: str | None = None,
    model: str | None = None,
    cost_budget: float = COST_OVERRUN_DEFAULT,
    started_at: float | None = None,
) -> list[Anomaly]:
    """Aggregate a completed session into ``platform_db`` and return anomalies (R2).

    Persists one ``agent_sessions`` summary row plus one ``agent_tool_calls`` row
    per step (with a redacted args digest), audits any dangerous-tool use (D4/R7),
    and runs anomaly detection (R4). Returns the detected anomalies so the caller
    (autopilot / dashboard) can alert or abort.
    """
    total_cost = sum(s.cost_usd for s in steps)
    in_tok = sum(s.input_tokens for s in steps)
    out_tok = sum(s.output_tokens for s in steps)
    errors = sum(0 if s.ok else 1 for s in steps)

    for i, s in enumerate(steps):
        digest = _redact(s.args)
        platform_db.record_agent_tool_call(
            session_id,
            s.tool,
            tenant=tenant,
            step=s.step if s.step is not None else i,
            args_digest=digest,
            ok=s.ok,
            error=s.error,
            latency_ms=s.latency_ms,
        )
        if s.tool in _DANGEROUS_TOOLS:  # R7 / D4
            _audit_dangerous(session_id, tenant, s.tool)

    anomalies = detect_anomalies_from_steps(steps, cost_usd=total_cost, cost_budget=cost_budget)
    status = "ok"
    if any(a.severity == "critical" for a in anomalies):
        status = "anomaly"
    elif errors:
        status = "error" if errors == len(steps) else status

    platform_db.record_agent_session(
        session_id,
        tenant=tenant,
        agent=agent,
        model=model,
        steps=len(steps),
        tool_calls=len(steps),
        errors=errors,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=total_cost,
        status=status,
        anomalies=[a.code for a in anomalies] or None,
        ended=True,
        started_at=_utc_stamp(started_at) if started_at is not None else None,
    )
    return anomalies


def _utc_stamp(epoch: float) -> str:
    """``YYYY-MM-DD HH:MM:SS`` UTC — the shape SQLite's ``CURRENT_TIMESTAMP`` writes."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def _audit_dangerous(session_id: str, tenant: str, tool: str) -> None:
    try:
        platform_db.write_audit_event(
            source="agentops",
            actor=f"agent:{tenant}",
            action="agent_dangerous_tool",
            target=tool,
            details={"session_id": session_id, "tool": tool},
        )
    except Exception:
        pass  # audit is best-effort bookkeeping (fail-open)


def detect_anomalies_from_steps(
    steps: Sequence[AgentStep],
    *,
    cost_usd: float | None = None,
    cost_budget: float = COST_OVERRUN_DEFAULT,
    loop_threshold: int = LOOP_REPEAT_THRESHOLD,
    step_threshold: int = STEP_BLOWUP_THRESHOLD,
) -> list[Anomaly]:
    """Pure anomaly detection over an in-memory step list (R4).

    - **loop**: the same ``(tool, redacted-args)`` pair repeated ``loop_threshold``+ times.
    - **step_blowup**: more than ``step_threshold`` steps.
    - **cost_overrun**: total cost above ``cost_budget``.
    - **error_burst**: every step in a >=3-step session failed.
    """
    out: list[Anomaly] = []

    counts: dict[tuple[str, str], int] = {}
    for s in steps:
        key = (s.tool, _redact(s.args))
        counts[key] = counts.get(key, 0) + 1
    for (tool, _digest), n in counts.items():
        if n >= loop_threshold:
            out.append(
                Anomaly(
                    code="loop",
                    severity="critical",
                    detail=f"tool '{tool}' repeated {n}x with identical args",
                    tool=tool,
                )
            )

    if len(steps) > step_threshold:
        out.append(
            Anomaly(
                code="step_blowup",
                severity="critical",
                detail=f"{len(steps)} steps exceeds threshold {step_threshold}",
            )
        )

    total = cost_usd if cost_usd is not None else sum(s.cost_usd for s in steps)
    if total > cost_budget:
        out.append(
            Anomaly(
                code="cost_overrun",
                severity="warn",
                detail=f"session cost ${total:.4f} exceeds budget ${cost_budget:.4f}",
            )
        )

    errs = sum(0 if s.ok else 1 for s in steps)
    if len(steps) >= 3 and errs == len(steps):
        out.append(
            Anomaly(
                code="error_burst",
                severity="critical",
                detail=f"all {len(steps)} tool calls failed",
            )
        )
    return out


def detect_anomalies(session_id: str, **kwargs: Any) -> list[Anomaly]:
    """Reconstruct a persisted session's steps and run detection (R4, spec §4).

    Reads back the stored ``agent_tool_calls`` (args already redacted at write
    time) and re-derives anomalies. Cost comes from the session summary row.
    """
    trace = platform_db.get_agent_session_trace(session_id)
    session = trace.get("session")
    steps = [
        AgentStep(
            tool=row["tool"],
            args=row.get("args_digest"),  # already a redacted digest
            ok=bool(row["ok"]),
            error=row.get("error"),
            latency_ms=row.get("latency_ms"),
            step=row.get("step"),
        )
        for row in trace.get("steps", [])
    ]
    cost = float(session["cost_usd"]) if session else 0.0
    return detect_anomalies_from_steps(steps, cost_usd=cost, **kwargs)


def tool_success_rate(tool: str, window: str = "all", *, tenant: str | None = None) -> float:
    """Success rate (0..1) for a tool (R2, GWT-2). ``window`` reserved for future use."""
    rows = platform_db.tool_success_rate(tool, tenant=tenant)
    for r in rows:
        if r["tool"] == tool:
            return r["success_rate"]
    return 0.0


@dataclass
class SessionRecorder:
    """Incremental helper for live instrumentation — collect steps, then flush.

    Usage::

        rec = SessionRecorder("sess-1", tenant="acme")
        rec.add(AgentStep("recall_memory", ok=True, cost_usd=0.001))
        anomalies = rec.flush()
    """

    session_id: str
    tenant: str = "default"
    agent: str | None = None
    model: str | None = None
    cost_budget: float = COST_OVERRUN_DEFAULT
    steps: list[AgentStep] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)  # so the session has a real duration

    def add(self, step: AgentStep) -> None:
        self.steps.append(step)

    def flush(self) -> list[Anomaly]:
        return record_session(
            self.session_id,
            self.tenant,
            self.steps,
            agent=self.agent,
            model=self.model,
            cost_budget=self.cost_budget,
            started_at=self.started_at,
        )


class CircuitBreakerTripped(RuntimeError):
    """Raised in-loop when a critical agent anomaly is detected (item 4.4)."""

    def __init__(self, anomaly: Anomaly) -> None:
        super().__init__(f"agent circuit-breaker tripped: {anomaly.code} — {anomaly.detail}")
        self.anomaly = anomaly


def warn_ratio_from_env() -> float:
    """The configured soft-threshold ratio (``EXAMLOPS_AGENT_BREAKER_WARN_RATIO``), clamped."""
    raw = os.getenv(WARN_RATIO_ENV, "").strip()
    try:
        value = float(raw) if raw else WARN_RATIO_DEFAULT
    except ValueError:
        value = WARN_RATIO_DEFAULT
    return min(max(value, 0.0), 0.99)


def detect_soft_warnings(
    steps: Sequence[AgentStep],
    *,
    warn_ratio: float,
    cost_budget: float = COST_OVERRUN_DEFAULT,
    loop_threshold: int = LOOP_REPEAT_THRESHOLD,
    step_threshold: int = STEP_BLOWUP_THRESHOLD,
) -> list[Anomaly]:
    """Pure pre-abort detection: what is *approaching* a hard threshold but has not crossed it.

    Codes are ``loop_warning`` / ``step_blowup_warning`` / ``cost_warning`` (severity ``warn``).
    A kind that has already crossed its hard threshold is not reported here — the hard detector
    owns that. ``warn_ratio`` of 0 disables the soft threshold.
    """
    if warn_ratio <= 0:
        return []
    out: list[Anomaly] = []
    counts: dict[tuple[str, str], int] = {}
    for s in steps:
        key = (s.tool, _redact(s.args))
        counts[key] = counts.get(key, 0) + 1
    loop_soft = max(2, math.ceil(warn_ratio * loop_threshold))
    for (tool, _digest), n in counts.items():
        if loop_soft <= n < loop_threshold:
            out.append(
                Anomaly(
                    "loop_warning",
                    "warn",
                    f"tool '{tool}' repeated {n}x with identical args (abort at {loop_threshold}x)",
                    tool,
                )
            )
    step_soft = math.ceil(warn_ratio * step_threshold)
    if step_soft <= len(steps) <= step_threshold:
        out.append(
            Anomaly(
                "step_blowup_warning",
                "warn",
                f"{len(steps)} steps, abort above {step_threshold}",
            )
        )
    total = sum(s.cost_usd for s in steps)
    if cost_budget > 0 and warn_ratio * cost_budget <= total <= cost_budget:
        out.append(
            Anomaly(
                "cost_warning",
                "warn",
                f"session cost ${total:.4f} is over {warn_ratio:.0%} of budget ${cost_budget:.4f}",
            )
        )
    return out


def _emit_breaker_event(kind: str, session_id: str | None, anomaly: Anomaly) -> None:
    """Audit a breaker warning/trip (``agent_breaker_warning`` / ``agent_breaker_tripped``).

    Fails open — telemetry never breaks the loop it watches — but a lost event is *counted*
    (``dropped_audit_events``), not hidden. The Prometheus exposition counts these rows, so the
    audit trail and the metric cannot disagree.
    """
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "agentops",
        "agent:breaker",
        f"agent_breaker_{kind}",
        anomaly.code,
        {"session_id": session_id, "detail": anomaly.detail, "tool": anomaly.tool},
    )


@dataclass
class AgentCircuitBreaker:
    """In-loop circuit-breaker wiring the pure detection into a live agent graph (item 4.4).

    Feed each tool step as it happens with :meth:`guard`; the breaker re-runs anomaly detection over
    the accumulating steps and **aborts the loop** (raises :class:`CircuitBreakerTripped`) the moment
    a *critical* anomaly appears — a runaway loop, step blow-up, or all-errors burst — instead of only
    noticing post-hoc. ``cost_overrun`` (a warning) trips only when ``abort_on_cost`` is set. This is
    the guardrail that stops an autopilot/agent from burning GPU-hours or looping forever.

    **Warn before abort (ADR 0021 decision 4).** At ``warn_ratio`` of each hard threshold the breaker
    emits a *warning* — once per kind per session — into :attr:`warnings`, through ``on_event`` if
    given, and as an ``agent_breaker_warning`` audit event; the trip itself is an
    ``agent_breaker_tripped`` audit event. ``warn_ratio=0`` turns warnings off and leaves the hard
    abort exactly as it was. ``session_id`` only labels the audit details.
    """

    cost_budget: float = COST_OVERRUN_DEFAULT
    loop_threshold: int = LOOP_REPEAT_THRESHOLD
    step_threshold: int = STEP_BLOWUP_THRESHOLD
    abort_on_cost: bool = True
    warn_ratio: float = field(default_factory=warn_ratio_from_env)
    session_id: str | None = None
    on_event: Callable[[str, Anomaly], None] | None = None
    emit_audit: bool = True
    steps: list[AgentStep] = field(default_factory=list)
    tripped_by: Anomaly | None = None
    warnings: list[Anomaly] = field(default_factory=list)

    def _critical(self) -> Anomaly | None:
        anomalies = detect_anomalies_from_steps(
            self.steps,
            cost_budget=self.cost_budget,
            loop_threshold=self.loop_threshold,
            step_threshold=self.step_threshold,
        )
        for a in anomalies:
            if a.severity == "critical" or (a.code == "cost_overrun" and self.abort_on_cost):
                return a
        return None

    def check(self) -> Anomaly | None:
        """Return the abort-worthy anomaly over the steps so far, or None."""
        return self._critical()

    def tripped(self) -> bool:
        return self.tripped_by is not None

    def _notify(self, kind: str, anomaly: Anomaly) -> None:
        if self.emit_audit:
            _emit_breaker_event(kind, self.session_id, anomaly)
        if self.on_event is not None:
            try:
                self.on_event(kind, anomaly)
            except Exception:
                pass  # a broken observer must not change the abort decision

    def _warn(self) -> None:
        seen = {w.code for w in self.warnings}
        for w in detect_soft_warnings(
            self.steps,
            warn_ratio=self.warn_ratio,
            cost_budget=self.cost_budget,
            loop_threshold=self.loop_threshold,
            step_threshold=self.step_threshold,
        ):
            if w.code in seen:
                continue
            seen.add(w.code)
            self.warnings.append(w)
            self._notify("warning", w)

    def guard(self, step: AgentStep) -> None:
        """Record ``step`` and abort the loop if it pushes the session into a critical anomaly."""
        self.steps.append(step)
        anomaly = self._critical()
        if anomaly is not None:
            self.tripped_by = anomaly
            self._notify("tripped", anomaly)
            raise CircuitBreakerTripped(anomaly)
        self._warn()


__all__ = [
    "AgentStep",
    "Anomaly",
    "AgentCircuitBreaker",
    "CircuitBreakerTripped",
    "SessionRecorder",
    "record_session",
    "detect_anomalies",
    "detect_anomalies_from_steps",
    "detect_soft_warnings",
    "warn_ratio_from_env",
    "tool_success_rate",
]
