from __future__ import annotations

import functools
from collections.abc import Callable

from langgraph.types import interrupt

WRITE_TOOLS: set[str] = set()

_AFFIRMATIVE = {"y", "yes", "ok", "okay", "approve", "confirm", "true", "1"}


def _is_affirmative(decision) -> bool:
    if isinstance(decision, bool):
        return decision
    return str(decision).strip().lower() in _AFFIRMATIVE


def confirmed_write(summary_fn: Callable[..., str]):
    """Wrap a write tool so it requests confirmation via interrupt() before acting.

    summary_fn receives the same keyword args as the tool and returns a one-line
    human-readable description of the pending action.
    """

    def decorator(fn):
        WRITE_TOOLS.add(fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            summary = summary_fn(*args, **kwargs)
            decision = interrupt({"action": fn.__name__, "args": kwargs, "summary": summary})
            if not _is_affirmative(decision):
                return "Cancelled — no action taken."
            return fn(*args, **kwargs)

        return wrapper

    return decorator
