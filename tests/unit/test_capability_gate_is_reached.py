"""A capability check that skips `iam_gate.enforce` skips step-up and the centre's PDP.

`capabilities.require_capability(cap)` is the enforcing dependency: it refuses the role that lacks
the capability **and then** calls `iam_gate.enforce` — RFC 9470 **step-up** for the designated
capabilities, and the federated centre's **PDP** asked about *that named capability* (ADR 0120).

A federated caller is never unchecked: `require_role`, which every router uses, already asks the
centre's PDP `api.read` / `api.write` for the route path. The difference is granularity. A centre
whose policy is written in capability terms — `model.promote`, which is how ADR 0120's own example
writes one — only bites where the fine-grained question is asked. (This docstring first claimed the
ungated routers reached no PDP at all; they do, coarsely.)

Checking the capability with a bare `can(role, cap)` therefore reuses the *name* of a gate without
the gate. `routers/challenger.py` did exactly that for `model.promote` — a step-up capability, and
one its own module docstring described as reusing the established gate — so with step-up enforced
`/api/models/…/alias` demanded re-authentication while `/api/challenger/{model}/promote`, which
promotes the same model to production, did not.

Two rules, of very different force:

* **Step-up capabilities must go through the enforcing dependency.** Not a ratchet — a route that
  guards one of these with a bare `can()` is a hole, and there is nothing to phase out.
* Everything else is a **ratchet**. No router now checks capabilities with `can()`, so the PDP sees
  only the coarse question for them; converting them one at a time is real work with a real risk
  (`require_capability` admits operators, so a careless swap widens who may act). They are listed
  so the number can only fall.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "platform" / "services" / "dashboard" / "backend"
ROUTERS = BACKEND / "routers"

#: Routers that check a capability with `can()` and never call the enforcing dependency, so a
#: federated user's centre PDP is not consulted for them. Remove an entry when you convert it.
UNGATED: set[str] = set()


def _step_up_capabilities() -> set[str]:
    """The capability constants named in `STEP_UP_CAPABILITIES`."""
    text = (BACKEND / "capabilities.py").read_text(encoding="utf-8")
    match = re.search(
        r"STEP_UP_CAPABILITIES:\s*frozenset\[str\]\s*=\s*frozenset\(\{([^}]*)\}\)", text
    )
    assert match, "STEP_UP_CAPABILITIES is no longer declared the way this guard reads it"
    caps = {c.strip() for c in match.group(1).split(",") if c.strip()}
    assert caps, "no step-up capabilities parsed — the guard would then check nothing"
    return caps


def _routers_using(pattern: str) -> set[str]:
    """Routers whose source matches `pattern`.

    Matched as a **call** (`can(`), never as a word: `\bcan\b` also matches the English "can",
    and four routers were reported as ungated purely because their prose used it.
    """
    files = sorted(ROUTERS.glob("*.py"))
    assert files, f"no routers under {ROUTERS} — the path is stale, not the tree clean"
    return {p.name for p in files if re.search(pattern, p.read_text(encoding="utf-8"))}


def test_every_step_up_capability_is_checked_through_the_enforcing_dependency():
    """The hole this guard was written for. Not a ratchet: there is nothing here to phase out."""
    offenders = []
    for cap in _step_up_capabilities():
        for path in sorted(ROUTERS.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            if not re.search(rf"\b{cap}\b", src):
                continue
            if "require_capability" not in src:
                offenders.append(f"{path.name} guards {cap}")
    assert not offenders, (
        "these routes guard a step-up capability without `require_capability`, so neither RFC 9470 "
        "step-up nor the centre's PDP ever runs for them:\n  " + "\n  ".join(offenders)
    )


def test_the_ungated_list_only_shrinks():
    # Two shapes reach `iam_gate.enforce`, and both are legitimate. Nearly every router names
    # its capability in a route-level `Depends(require_capability(...))`. `cli.py` cannot —
    # its capability is chosen per request from the tier of the `exa` command being run — so
    # it calls `enforce` itself once the answer exists. A router using neither reaches no
    # capability-level check at all.
    gated = _routers_using(r"\brequire_capability\(") | _routers_using(r"\benforce\(")
    using_can = _routers_using(r"\bcan\(") - gated
    new = sorted(using_can - UNGATED)
    assert not new, (
        "these routers check a capability with a bare `can()` and never reach `iam_gate.enforce`, "
        f"so a federated user's centre PDP is not consulted: {new}. Prefer "
        "`Depends(require_capability(CAP))`; if the route must stay role-gated as well, keep both "
        "dependencies rather than swapping one for the other."
    )
    fixed = sorted(UNGATED - using_can)
    assert not fixed, f"{fixed} are converted — remove them from UNGATED so the count keeps falling"


def test_the_guard_can_still_see_the_enforcing_dependency():
    """Anti-vacuity: if the names moved, both assertions above would pass on nothing."""
    assert "models.py" in _routers_using(r"\brequire_capability\(")
    assert "challenger.py" in _routers_using(r"\brequire_capability\(")
