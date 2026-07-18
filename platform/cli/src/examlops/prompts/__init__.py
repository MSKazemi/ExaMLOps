"""B1 — Prompt registry client (ADR 0009, spec B1-prompt-management).

Immutable prompt versions + moving labels (``dev``/``staging``/``prod``) resolved by
``name@label``, backed by ``platform_db`` (works with no external service, spec R4).
The client caches with a short TTL and **fails safe to the last-known-good version**
if the registry is unreachable (spec R6).

Rendering validates that every declared variable is supplied and raises *before* any
model call (spec R2/R3); variables are substituted strictly as data (spec R12).
"""

from __future__ import annotations

import json
import string
import time
from dataclasses import dataclass, field
from typing import Any

_CACHE_TTL_S = 30.0
# (name, label) -> (PromptVersion, fetched_at). Also serves as last-known-good store.
_cache: dict[tuple[str, str], tuple[PromptVersion, float]] = {}


@dataclass
class PromptVersion:
    name: str
    version: int
    template: str
    variables: list[str] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)
    actor: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PromptVersion:
        return cls(
            name=row["name"],
            version=int(row["version"]),
            template=row["template"],
            variables=json.loads(row.get("variables") or "[]"),
            tags=json.loads(row.get("tags") or "{}"),
            actor=row.get("actor"),
            created_at=row.get("created_at", ""),
        )


def declared_variables(template: str) -> list[str]:
    """Extract ``{var}`` names from a template (for auto-declaring variables)."""
    seen: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name and field_name not in seen:
            seen.append(field_name)
    return seen


def render(pv: PromptVersion, **variables: Any) -> str:
    """Render a prompt version, validating variables first (spec R2/R3/R12).

    Raises ``ValueError`` naming any missing variable *before* any model call. Extra
    variables are ignored. Substitution treats values strictly as data.
    """
    required = set(pv.variables) or set(declared_variables(pv.template))
    missing = [v for v in required if v not in variables]
    if missing:
        raise ValueError(f"prompt '{pv.name}' v{pv.version} missing variable(s): {missing}")
    # format_map with a defaultdict-free dict: unknown fields in the template that were
    # not declared would raise KeyError — surface as a clear error rather than a crash.
    try:
        return pv.template.format(**variables)
    except KeyError as exc:  # a template placeholder with no declared/ supplied value
        raise ValueError(f"prompt '{pv.name}' template references undeclared {exc}") from exc


def get_prompt(name: str, label: str = "prod") -> PromptVersion:
    """Resolve ``name@label`` with a short-TTL cache + last-known-good fail-safe (R5/R6).

    Raises ``LookupError`` only when the prompt is unknown *and* nothing is cached.
    """
    key = (name, label)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and (now - cached[1]) < _CACHE_TTL_S:
        return cached[0]
    try:
        from examlops.data.prompts import get_prompt_by_label

        row = get_prompt_by_label(name, label)
        if row is None:
            raise LookupError(f"no prompt '{name}@{label}'")
        pv = PromptVersion.from_row(row)
        _cache[key] = (pv, now)
        return pv
    except Exception:
        # Registry unreachable or lookup failed — fail safe to last-known-good (R6).
        if cached:
            return cached[0]
        raise


def clear_cache() -> None:
    _cache.clear()
