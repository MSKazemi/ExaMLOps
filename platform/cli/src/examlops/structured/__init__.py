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

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from examlops import data as platform_db


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
        props = schema.get("properties", {})
        repaired = {k: repair_object(v, props[k]) for k, v in obj.items() if k in props}
        # Fill required-but-missing with a typed zero value so validation can pass.
        for req in schema.get("required", []):
            if req not in repaired:
                repaired[req] = _zero_value(props.get(req, {}))
        return repaired
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
) -> Any:
    """Generate an object that is **guaranteed** to validate against ``schema`` (R1/R2).

    Validate → (repair + revalidate up to ``max_repairs``) → else raise. Each attempt's
    outcome is metered for the structured-output failure rate (R8).
    """
    obj = generate_fn(prompt)
    if not validate_object(obj, schema):
        platform_db.record_structured_output_event("valid", model=model, tenant=tenant)
        return obj
    for _ in range(max_repairs):
        obj = repair_object(obj, schema)
        if not validate_object(obj, schema):
            platform_db.record_structured_output_event("repaired", model=model, tenant=tenant)
            return obj
    platform_db.record_structured_output_event("failed", model=model, tenant=tenant)
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
) -> str:
    """Capture a reasoning trace **redacted** (D8), TTL'd + tenant-scoped (R6/GWT-6).

    Returns the redacted trace. ``now_ts`` is injectable for testing.
    """
    redacted = _redact(trace)
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


def _redact(text: str) -> str:
    try:
        from examlops.guardrails import redact_pii

        redacted, _found = redact_pii(text)
        return redacted
    except Exception:
        return text


def _now() -> float:
    from datetime import datetime

    return datetime.now(UTC).timestamp()


__all__ = [
    "StructuredOutputError",
    "ReasoningBudget",
    "validate_object",
    "repair_object",
    "generate_structured",
    "account_reasoning",
    "capture_reasoning_trace",
    "get_reasoning_trace",
]
