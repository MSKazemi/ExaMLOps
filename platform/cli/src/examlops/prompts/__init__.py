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
import random
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


def _weighted_choice(split: list[dict[str, Any]], rng: random.Random | None = None) -> int:
    versions = [int(r["version"]) for r in split]
    weights = [float(r["weight"]) for r in split]
    chooser = rng or random
    return int(chooser.choices(versions, weights=weights, k=1)[0])


def get_prompt(
    name: str, label: str = "prod", *, rng: random.Random | None = None
) -> PromptVersion:
    """Resolve ``name@label`` with a short-TTL cache + last-known-good fail-safe (R5/R6).

    **BL-109 canary split.** When ``name@label`` has a weighted split configured
    (``exa prompt canary``), a version is drawn fresh on *every call* in proportion to the
    configured weights — never cached — so concurrent callers genuinely see traffic divided
    per the split rather than one version "winning" a whole cache window. The one extra
    indexed lookup this costs on every resolution (split or not) is the price of that
    correctness; it is a single-row SQLite read keyed on the same ``(name, label)`` pair the
    label lookup already uses. The resolved split choice still seeds the last-known-good
    fallback below, so a registry outage mid-split degrades to whichever version was drawn
    most recently rather than failing the call.

    Raises ``LookupError`` only when the prompt is unknown *and* nothing is cached.
    """
    key = (name, label)
    now = time.monotonic()
    cached = _cache.get(key)
    try:
        from examlops.data.prompts import get_prompt_by_label, get_prompt_split, get_prompt_version

        split = get_prompt_split(name, label)
        if split:
            row = get_prompt_version(name, _weighted_choice(split, rng))
        elif cached and (now - cached[1]) < _CACHE_TTL_S:
            return cached[0]
        else:
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


def migrate_prompts(
    to: str = "mlflow", *, source: str = "platform_db", dry_run: bool = False
) -> dict[str, Any]:
    """Copy every prompt — all versions in order, then every label — from ``source`` to ``to``.

    Version numbers must survive the move: a label, an audit event or a lineage node that says
    ``skipper-system@prod = v3`` has to mean the same v3 afterwards. A registry numbers versions
    itself, so a prompt that already exists at the destination is **skipped**, never merged — merging
    would renumber it. After each create the returned number is checked against the source's, and a
    mismatch stops the migration rather than leaving a shifted history behind.
    """
    from examlops.data import prompts as store

    if to == source:
        raise ValueError("source and destination backends are the same")
    with store.use_backend(source):
        names = store.list_prompt_names()
        plan = {
            n: (
                sorted(store.list_prompt_versions(n), key=lambda r: int(r["version"])),
                store.list_prompt_labels(n),
            )
            for n in names
        }
    with store.use_backend(to):
        existing = set(store.list_prompt_names())
    report: dict[str, Any] = {
        "source": source,
        "destination": to,
        "dry_run": dry_run,
        "migrated": [],
        "skipped": [],
        "versions": 0,
        "labels": 0,
    }
    for name, (versions, labels) in plan.items():
        if name in existing:
            report["skipped"].append(
                {"name": name, "reason": "already in the destination — merging would renumber it"}
            )
            continue
        report["migrated"].append(name)
        report["versions"] += len(versions)
        report["labels"] += len(labels)
        if dry_run:
            continue
        with store.use_backend(to):
            for row in versions:
                got = store.create_prompt_version(
                    name,
                    row["template"],
                    variables=json.loads(row.get("variables") or "[]"),
                    tags=json.loads(row.get("tags") or "{}"),
                    actor=row.get("actor"),
                )
                if int(got) != int(row["version"]):
                    raise RuntimeError(
                        f"prompt '{name}': source v{row['version']} became v{got} in {to} — "
                        "stopping before any label points at the wrong version"
                    )
            for lab in labels:
                store.set_prompt_label(name, lab["label"], int(lab["version"]))
    clear_cache()
    return report
