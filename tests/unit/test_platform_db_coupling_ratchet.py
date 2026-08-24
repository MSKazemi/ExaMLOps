"""Data-layer coupling ratchet (enterprise-readiness Phase 4, item 4.1).

The audit's cohesion finding: ~96 modules reach directly into `platform_db` instead of the semver'd
`examlops.sdk` facade, so the data layer can't evolve without breaking callers. A full migration is a
tracked refactor; this guard stops the coupling from GROWING in the meantime — a **ratchet**: the
count of non-data-layer modules importing `platform_db` may only go DOWN. A new direct importer fails
CI (route it through `examlops.sdk`); refactoring one onto the SDK lowers the baseline.

The data layer itself — `platform_db.py`, the `storage/` backends, and the `sdk/` facade — is exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "platform" / "cli" / "src" / "examlops"

# Ratchet: only ever LOWER this as call sites migrate onto examlops.sdk. Never raise it — the one
# exception is landing new *domain-layer* modules that genuinely own data access (this baseline was
# set at 97 after the Phase 1–5 wave added admission/events/coordination/backup/forecast/… ). New
# *presentation* code (CLI leaf commands, dashboard) must still go through the SDK, not platform_db.
# COMPLETE (items 4.5/4.1): every public-API call site now goes through the per-domain examlops.data.*
# facades — ZERO modules import a public platform_db helper directly. Kept at 0 as a hard guard: no new
# code may reach into the monolith's public surface. (Private-internal + prose refs are not counted.)
BASELINE_MAX = 0

_EXEMPT = ("platform_db.py",)  # the module itself
_EXEMPT_DIRS = ("storage", "sdk", "data")  # the data-layer backends + the SDK + per-domain facades

# Attribute-style access reaches the whole public surface → always counts.
_ATTR_RE = re.compile(
    r"^\s*(?:import\s+examlops\.platform_db\b|from\s+examlops\s+import\s+platform_db\b)", re.M
)
# `from examlops.platform_db import <names>` (single or parenthesized multiline).
_FROM_RE = re.compile(r"^\s*from\s+examlops\.platform_db\s+import\s+(\([^)]*\)|[^\n]*)", re.M)


def _imports_public_helper(text: str) -> bool:
    """True if the file imports a PUBLIC platform_db helper (not just private internals / prose)."""
    if _ATTR_RE.search(text):
        return True
    for m in _FROM_RE.finditer(text):
        body = m.group(1).strip().strip("()")
        for part in body.split(","):
            name = part.split(" as ")[0].strip()
            if name and not name.startswith("_"):  # a public helper still coupled to the monolith
                return True
    return False


def _importers() -> list[str]:
    """Modules importing a public `platform_db` helper — and proof there was something to read.

    The ratchet's baseline is 0, so its entire content is a negative claim, and an empty scan
    produces the strongest possible pass: zero importers found, `0 <= 0`, green. Demonstrated by
    pointing `_ROOT` at a directory that does not exist — both tests in this module passed.
    That failure is indistinguishable from total compliance, and this repo has already moved this
    tree once (into `platform/`), which is exactly how a root goes stale.
    """
    files = [p for p in _ROOT.rglob("*.py") if "__pycache__" not in p.parts]
    assert files, (
        f"scanned {_ROOT} and found no Python files — the ratchet's root is stale, not the tree "
        "clean. Until this path is right the guard enforces nothing."
    )
    out: list[str] = []
    for py in files:
        rel = py.relative_to(_ROOT)
        if rel.name in _EXEMPT or (rel.parts and rel.parts[0] in _EXEMPT_DIRS):
            continue
        if "__pycache__" in rel.parts:
            continue
        if _imports_public_helper(py.read_text(encoding="utf-8")):
            out.append(str(rel))
    return out


def test_platform_db_coupling_does_not_grow():
    importers = _importers()
    assert len(importers) <= BASELINE_MAX, (
        f"{len(importers)} modules import platform_db directly (baseline {BASELINE_MAX}). "
        "New code must go through examlops.sdk, not platform_db. If you migrated a call site, "
        f"LOWER BASELINE_MAX to {len(importers)}.\nCurrent importers:\n  "
        + "\n  ".join(sorted(importers))
    )


def test_ratchet_is_tight():
    """Keep the baseline honest — if the count dropped, the baseline must be lowered to match."""
    count = len(_importers())
    assert BASELINE_MAX - count <= 3, (
        f"platform_db coupling fell to {count}; lower BASELINE_MAX from {BASELINE_MAX} to keep the "
        "ratchet tight (so it actually catches the next new importer)."
    )
