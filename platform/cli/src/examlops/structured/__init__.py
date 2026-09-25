"""Next-Gen 40 · B8 — structured output & reasoning ops (ADR 0035).

Two capabilities the gateway (B2) / engine (E2) offer to Skipper, RAG (B4), and extraction:

**Structured output** — schema-constrained generation that is **guaranteed valid**:
``generate_structured`` runs a generator, validates the result against a JSON Schema, and on
the rare invalid output **repairs** (coerce types, drop extras) and retries before failing
explicitly (R1/R2/GWT-1/GWT-2). Every attempt is metered (valid/repaired/failed).

**Reasoning ops** — a per-request/-model **reasoning budget** is enforced (thinking cut off
at the limit, R4/GWT-4); reasoning vs output tokens/cost are **accounted separately** for C1
telemetry + FinOps (R5/GWT-5); reasoning **traces** are captured **redacted** (D8), TTL'd,
and tenant-scoped (R6/GWT-6).

Uses ``jsonschema`` when installed and degrades to a minimal built-in validator otherwise —
no LLM, engine, or provider API required to validate, repair, budget, or account.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from examlops import data as platform_db
from examlops.engines.config import reasoning_tokens_from_usage


class StructuredOutputError(RuntimeError):
    """Raised when a structured output cannot be made valid even after repair (R2)."""


@dataclass
class ReasoningBudget:
    max_thinking_tokens: int

    def enforce(self, requested_thinking_tokens: int) -> tuple[int, bool]:
        """Cap thinking at the budget; returns (allowed, was_cut) (R4/GWT-4)."""
        if requested_thinking_tokens > self.max_thinking_tokens:
            return self.max_thinking_tokens, True
        return requested_thinking_tokens, False


def validate_object(obj: Any, schema: dict[str, Any]) -> list[str]:
    """Return a list of validation errors ([] = valid). Uses jsonschema if available."""
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(schema)
        return [e.message for e in validator.iter_errors(obj)]
    except ImportError:
        return _minimal_validate(obj, schema)


def _minimal_validate(obj: Any, schema: dict[str, Any]) -> list[str]:
    """Dependency-free fallback: type + required + basic property types."""
    errors: list[str] = []
    stype = schema.get("type")
    type_map: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
    }
    if stype in type_map and not isinstance(obj, type_map[stype]):
        errors.append(f"expected type {stype}, got {type(obj).__name__}")
        return errors
    if stype == "object" and isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errors.append(f"missing required property {req!r}")
        for key, subschema in schema.get("properties", {}).items():
            if key in obj:
                errors.extend(_minimal_validate(obj[key], subschema))
    return errors


def repair_object(obj: Any, schema: dict[str, Any]) -> Any:
    """Best-effort repair toward the schema: coerce scalar types, drop unknown props (R2)."""
    stype = schema.get("type")
    if stype == "object" and isinstance(obj, dict):
        if "properties" not in schema:
            # A free-form object (``{"type": "object"}``, e.g. tool-call arguments) declares no
            # properties to keep, so "drop unknown" would empty it: there is nothing to repair.
            return obj
        props = schema.get("properties", {})
        repaired = {k: repair_object(v, props[k]) for k, v in obj.items() if k in props}
        # Fill required-but-missing with a typed zero value so validation can pass.
        for req in schema.get("required", []):
            if req not in repaired:
                repaired[req] = _zero_value(props.get(req, {}))
        return repaired
    if stype == "array" and isinstance(obj, list) and isinstance(schema.get("items"), dict):
        return [repair_object(v, schema["items"]) for v in obj]
    if stype == "integer":
        try:
            return int(obj)
        except (TypeError, ValueError):
            return 0
    if stype == "number":
        try:
            return float(obj)
        except (TypeError, ValueError):
            return 0.0
    if stype == "string" and not isinstance(obj, str):
        return str(obj)
    if stype == "boolean":
        return bool(obj)
    return obj


def _zero_value(schema: dict[str, Any]) -> Any:
    zeros: dict[str, Any] = {
        "object": {},
        "array": [],
        "string": "",
        "number": 0.0,
        "integer": 0,
        "boolean": False,
    }
    stype = schema.get("type")
    # A schema with no `type` (or a non-string one) has no zero value, which is exactly what
    # the None default means — looking it up with an unhashable key would raise instead.
    return zeros.get(stype) if isinstance(stype, str) else None


def generate_structured(
    prompt: str,
    schema: dict[str, Any],
    *,
    generate_fn: Callable[[str], Any],
    max_repairs: int = 1,
    model: str = "-",
    tenant: str = "default",
    constrained: bool = False,
) -> Any:
    """Generate an object that is **guaranteed** to validate against ``schema`` (R1/R2).

    Validate → (repair + revalidate up to ``max_repairs``) → else raise. Each attempt's
    outcome is metered for the structured-output failure rate (R8). ``constrained`` says whether
    the schema constrained the decoder (ADR 0035 clause 1) or only judged the answer, so the
    failure rate can be read per decoding mode — a constrained backend that still needs repairs
    is not honouring the constraint.
    """
    obj = generate_fn(prompt)
    meter = {"model": model, "tenant": tenant, "constrained": constrained}
    if not validate_object(obj, schema):
        platform_db.record_structured_output_event("valid", **meter)
        return obj
    for _ in range(max_repairs):
        obj = repair_object(obj, schema)
        if not validate_object(obj, schema):
            platform_db.record_structured_output_event("repaired", **meter)
            return obj
    platform_db.record_structured_output_event("failed", **meter)
    raise StructuredOutputError(
        f"output failed schema validation after {max_repairs} repair(s): "
        f"{validate_object(obj, schema)}"
    )


def account_reasoning(
    model: str,
    reasoning_tokens: int,
    output_tokens: int,
    *,
    reasoning_rate: float = 0.0,
    output_rate: float = 0.0,
    tenant: str = "default",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Account reasoning vs output tokens/cost **separately** (R5/GWT-5)."""
    reasoning_cost = reasoning_tokens * reasoning_rate
    output_cost = output_tokens * output_rate
    platform_db.record_reasoning_usage(
        model,
        reasoning_tokens,
        output_tokens,
        reasoning_cost,
        output_cost,
        tenant=tenant,
        request_id=request_id,
    )
    return {
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "reasoning_cost": round(reasoning_cost, 6),
        "output_cost": round(output_cost, 6),
        "total_cost": round(reasoning_cost + output_cost, 6),
    }


def capture_reasoning_trace(
    request_id: str,
    trace: str,
    *,
    tenant: str = "default",
    ttl_seconds: float | None = None,
    now_ts: float | None = None,
) -> str | None:
    """Capture a reasoning trace **redacted** (ADR 0148 d2), TTL'd + tenant-scoped (R6/GWT-6).

    A reasoning trace is content, so it leaves through the same tenant redaction policy as span
    prompts and completions (``guardrails.telemetry_redactor``). **Fail closed:** if the redactor
    cannot be built or raises, nothing is stored, the loss is counted with the other dropped
    captures (``genai.redaction_failures()``) and ``None`` is returned - an unredacted trace is
    never written because the thing meant to clean it broke. Returns the redacted trace.
    ``now_ts`` is injectable for testing.
    """
    redacted = _redact(trace, tenant)
    if redacted is None:
        return None
    expires_at = None
    if ttl_seconds is not None:
        base = now_ts if now_ts is not None else _now()
        expires_at = base + ttl_seconds
    platform_db.store_reasoning_trace(request_id, redacted, tenant=tenant, expires_at=expires_at)
    return redacted


def get_reasoning_trace(request_id: str, *, now_ts: float | None = None) -> str | None:
    """Return the redacted trace if present and not TTL-expired, else None (R6)."""
    row = platform_db.get_reasoning_trace(request_id)
    if not row:
        return None
    expires_at = row.get("expires_at")
    if expires_at is not None:
        now = now_ts if now_ts is not None else _now()
        if now >= expires_at:
            return None
    return row["redacted_trace"]


def _redact(text: str, tenant: str = "default") -> str | None:
    try:
        from examlops.guardrails import telemetry_redactor

        out = telemetry_redactor(tenant)(text)
        if isinstance(out, str):
            return out
    except Exception:  # noqa: BLE001 - fail closed, counted
        pass
    try:
        from examlops.telemetry import genai

        genai.note_redaction_failure()
    except Exception:  # noqa: BLE001
        pass
    return None


def _now() -> float:
    from datetime import datetime

    return datetime.now(UTC).timestamp()


# -- Gateway enforcement (ADR 0035 clause 2) ------------------------------------------------

#: ``enforce`` (default) refuses a response that spent more thinking than its budget allows;
#: ``strict`` also refuses one whose backend reported no reasoning usage at all; ``flag`` serves it
#: and records the breach; ``off`` records nothing and refuses nothing. It only ever acts on a
#: request that has a budget - with none configured the gateway behaves exactly as before.
_MODES = ("enforce", "strict", "flag", "off")


def reasoning_budget_mode() -> str:
    """The over-budget policy. An unrecognised value falls back to ``enforce``, never ``off``."""
    mode = os.getenv("EXAMLOPS_REASONING_BUDGET_MODE", "enforce").strip().lower()
    return mode if mode in _MODES else "enforce"


@dataclass(frozen=True)
class ResolvedBudget:
    max_thinking_tokens: int
    source: str  # request | key | project | model | route | default


def resolve_reasoning_budget(
    model: str,
    *,
    tenant: str = "default",
    key_hash: str | None = None,
    project: str | None = None,
    requested: int | None = None,
) -> ResolvedBudget | None:
    """The applicable budget for one request, or ``None`` when nothing constrains it.

    Candidates: the caller's own ``requested`` cap, a cap on the virtual key, on its project, on
    the model, the route default in ``structured.yaml`` (clause 3), and the
    ``EXAMLOPS_REASONING_BUDGET_DEFAULT`` floor. **The tightest wins** - a
    narrower scope can tighten a broader one but never loosen it, so a per-key exception cannot
    quietly lift a model-wide limit. A store that cannot be read is skipped, not fatal: the
    request is served and the unenforced budget is the visible cost of an unreadable store.
    """
    from examlops.data import reasoning_budgets as store

    candidates: list[ResolvedBudget] = []
    if requested is not None and requested >= 0:
        candidates.append(ResolvedBudget(int(requested), "request"))
    try:
        for row in store.applicable(tenant, key_hash=key_hash, project=project, model=model):
            candidates.append(ResolvedBudget(int(row["max_thinking_tokens"]), row["scope"]))
    except Exception:  # noqa: BLE001
        pass
    # ADR 0035 clause 3: a per-route default from ``structured.yaml`` is one more candidate.
    try:
        from examlops.structured.policy import route_defaults

        route_cap = route_defaults(model).reasoning_budget
        if route_cap is not None:
            candidates.append(ResolvedBudget(route_cap, "route"))
    except Exception:  # noqa: BLE001 - same rule as an unreadable store: skipped, not fatal
        pass
    default = os.getenv("EXAMLOPS_REASONING_BUDGET_DEFAULT", "").strip()
    if default.isdigit():
        candidates.append(ResolvedBudget(int(default), "default"))
    return min(candidates, key=lambda c: c.max_thinking_tokens) if candidates else None


def judge_reasoning(
    budget: ResolvedBudget | None, observed: int | None, mode: str
) -> tuple[str, bool]:
    """Classify one response against its budget: ``(outcome, refuse)``.

    Outcomes: ``none`` (no budget or mode off), ``within``, ``exceeded``, ``unknown`` (the backend
    reported no reasoning usage). ``unknown`` is never a pass: it is recorded as its own outcome
    and, under ``strict``, refused.
    """
    if budget is None or mode == "off":
        return "none", False
    if observed is None:
        return "unknown", mode == "strict"
    if observed > budget.max_thinking_tokens:
        return "exceeded", mode in ("enforce", "strict")
    return "within", False


__all__ = [
    "StructuredOutputError",
    "ReasoningBudget",
    "ResolvedBudget",
    "judge_reasoning",
    "reasoning_budget_mode",
    "reasoning_tokens_from_usage",
    "resolve_reasoning_budget",
    "validate_object",
    "repair_object",
    "generate_structured",
    "account_reasoning",
    "capture_reasoning_trace",
    "get_reasoning_trace",
]
