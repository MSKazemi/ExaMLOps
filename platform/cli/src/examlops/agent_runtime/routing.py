"""Session affinity and session canary assignment (ADR 0144 d4, ADR 0146 d4). Pure.

**Affinity is on the runtime's own session key** - the thread id - never on a tool-protocol
header (MCP 2026-07-28 removed ``Mcp-Session-Id``). Rendezvous (highest-random-weight) hashing
picks the owning worker: every node computes the same owner from the same worker list with no
coordination, and adding or removing one worker moves only the sessions that hashed to it
(about ``1/n``), so an agent's working set, sandbox and the model server's KV cache stay put.

**Canary assignment is by new session.** A session's bucket is a stable hash of its id, so the
same id always lands on the same side, and the decision is made once, when the session opens -
the runtime pins the chosen version on the thread, and no later change to the canary share moves
an existing session.
"""

from __future__ import annotations

import hashlib

__all__ = ["canary_bucket", "owner", "starts_on_canary"]


def _h(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x00".join(parts).encode()).digest()[:8], "big")


def owner(session_key: str, workers: list[str] | tuple[str, ...]) -> str:
    """The worker that owns ``session_key`` (rendezvous hashing). ``ValueError`` if no workers."""
    if not workers:
        raise ValueError("no workers to route to")
    return max(sorted(set(workers)), key=lambda w: _h(w, session_key))


def canary_bucket(session_key: str) -> float:
    """A stable value in ``[0, 100)`` for ``session_key``."""
    return (_h("canary", session_key) % 1_000_000) / 10_000.0


def starts_on_canary(session_key: str, percent: float) -> bool:
    """Whether a NEW session with this key starts on the Canary version at ``percent`` %."""
    return percent > 0 and canary_bucket(session_key) < min(percent, 100.0)
