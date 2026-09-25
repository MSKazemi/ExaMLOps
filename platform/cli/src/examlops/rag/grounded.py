"""Grounded, schema-valid RAG answers through B8 structured output (ADR 0019 decision 2).

A free-text RAG answer can claim anything and cite nothing. With ``structured=True`` the query
asks the generator for a JSON object that names the numbered context chunks it used, and the object
is produced by :func:`examlops.structured.generate_structured` — validated against
:data:`GROUNDED_ANSWER_SCHEMA`, repaired once if invalid, and metered as valid / repaired / failed
in the same ``structured_output_events`` table every other B8 caller feeds.

Schema validity is not grounding, so a second, RAG-specific check follows: every cited chunk number
must name a chunk that was actually retrieved. Out-of-range or non-integer citations are **dropped
and reported**, never silently kept, and the answer is marked ``grounded`` only when at least one
valid citation survives — or when the model explicitly says the context was insufficient, which is
the correct answer to a question the knowledge base cannot answer, not a failure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

GROUNDED_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
        "insufficient_context": {"type": "boolean"},
    },
    "required": ["answer", "citations"],
}

STRUCTURED_INSTRUCTION = (
    "\n\nRespond with ONLY a JSON object, no prose around it: "
    '{"answer": "<answer>", "citations": [<numbers of the context chunks used>], '
    '"insufficient_context": <true if the context does not contain the answer>}'
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass
class GroundedAnswer:
    answer: str
    citations: list[int]
    insufficient_context: bool
    grounded: bool
    dropped_citations: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": self.citations,
            "insufficient_context": self.insufficient_context,
            "grounded": self.grounded,
            "dropped_citations": self.dropped_citations,
        }


def parse_json_answer(raw: Any) -> Any:
    """Turn a generator's raw output into the object B8 validates.

    A dict passes through. A string is parsed as JSON — code fences stripped, and the outermost
    ``{...}`` tried when the model wrapped the object in prose. Text that is not JSON at all (the
    offline echo generator, or a model that ignored the instruction) becomes ``{"answer": text}``:
    B8 then *repairs* it (``citations`` filled as ``[]``, metered as ``repaired``) and the grounding
    check marks it ungrounded — visible, not an exception.
    """
    if isinstance(raw, dict):
        return raw
    text = str(raw if raw is not None else "").strip()
    candidates = [_FENCE.sub("", text).strip()]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return {"answer": text}


def check_grounding(obj: dict[str, Any], n_contexts: int) -> GroundedAnswer:
    """Keep only citations that name a retrieved chunk (1-based) and decide ``grounded``."""
    valid: list[int] = []
    dropped: list[Any] = []
    for c in obj.get("citations") or []:
        # Only an integral value names a chunk. ``int()`` alone would truncate 2.7 to chunk 2,
        # turning a citation the model never made into a grounded one. Accepted: an int, an
        # integral float (JSON ``2.0``) and an all-digit string (``"2"``, a common model habit).
        if isinstance(c, bool):
            dropped.append(c)
            continue
        if isinstance(c, int):
            num = c
        elif isinstance(c, float) and c.is_integer():
            num = int(c)
        elif isinstance(c, str) and c.strip().isascii() and c.strip().isdigit():
            num = int(c.strip())
        else:
            dropped.append(c)
            continue
        if not 1 <= num <= n_contexts or num in valid:
            dropped.append(c)
            continue
        valid.append(num)
    insufficient = bool(obj.get("insufficient_context", False))
    return GroundedAnswer(
        answer=str(obj.get("answer", "")),
        citations=valid,
        insufficient_context=insufficient,
        grounded=bool(valid) or insufficient,
        dropped_citations=dropped,
    )


def generate_grounded(
    prompt: str,
    n_contexts: int,
    *,
    generate_fn: Callable[[str], Any],
    model: str = "rag",
    tenant: str = "default",
) -> GroundedAnswer:
    """Generate via B8 (validate → repair → meter) then enforce citation grounding.

    Raises :class:`examlops.structured.StructuredOutputError` if the object cannot be made valid.
    """
    from examlops.structured import generate_structured

    obj = generate_structured(
        prompt + STRUCTURED_INSTRUCTION,
        GROUNDED_ANSWER_SCHEMA,
        generate_fn=lambda p: parse_json_answer(generate_fn(p)),
        model=model,
        tenant=tenant,
    )
    return check_grounding(obj, n_contexts)


__all__ = [
    "GROUNDED_ANSWER_SCHEMA",
    "GroundedAnswer",
    "check_grounding",
    "generate_grounded",
    "parse_json_answer",
]
