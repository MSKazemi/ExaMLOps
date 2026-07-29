"""SM3 — hard intent-gate on long-term memory retrieval (ADR 0034).

``recall_memory`` must surface *learned* knowledge (procedures, past incidents, operator
preferences, stable facts) — never **current** platform state. Fetching live state from
memory would return stale, misleading answers; that is what the dedicated live tools
(``exa status`` / drift / cost / audit) are for.

Until now this was only a soft instruction in the tool's docstring. This module turns it
into a **hard gate**: a pure, dependency-free check the recall tool consults before it
touches the store. It is deliberately conservative — it blocks a recall only when a
temporal/live marker co-occurs with a platform-state noun, so genuine "procedure for
handling drift"-style queries still retrieve.
"""

from __future__ import annotations

# Temporal / "live" markers that signal a request for the state *right now*.
_LIVE_MARKERS = (
    "current",
    "currently",
    "right now",
    "now",
    "latest",
    "today",
    "at the moment",
    "as of now",
    "live ",
    "present",
    "these days",
)

# Platform-state nouns — things that have a live value queried via the dedicated tools.
_STATE_NOUNS = (
    "version",
    "drift",
    "cost",
    "spend",
    "budget",
    "audit",
    "status",
    "metric",
    "replica",
    "traffic",
    "deployment",
    "gpu",
    "latency",
    "alias",
    "promotion",
    "carbon",
)

_MIN_QUERY_LEN = 3

_REDIRECT = (
    "Memory recall is for learned procedures, past incidents, and operator preferences — "
    "not current platform state. Query live state with the dedicated tools "
    "(exa status / drift / cost / audit) instead of recalling from memory."
)


def retrieval_allowed(query: str, kind: str | None = None) -> tuple[bool, str]:
    """Decide whether a memory recall should proceed (SM3 hard intent-gate).

    Returns ``(allowed, reason)``. When ``allowed`` is False, ``reason`` is a message the
    recall tool returns verbatim so the agent redirects to live tools instead of surfacing
    stale memories. When True, ``reason`` is empty.
    """
    q = (query or "").strip().lower()
    if len(q) < _MIN_QUERY_LEN:
        return False, "Recall query is empty or too short — provide a specific question."
    has_marker = any(m in q for m in _LIVE_MARKERS)
    has_state = any(n in q for n in _STATE_NOUNS)
    if has_marker and has_state:
        return False, _REDIRECT
    return True, ""
