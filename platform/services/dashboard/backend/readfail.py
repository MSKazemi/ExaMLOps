"""A read that failed must not be served as a read that found nothing.

Several console reads answer a question whose empty answer is itself a **claim**: no model is in
scope of the EU AI Act, no model is drifting, no SLO is defined. Those routes wrapped their query in
``except Exception: return []``, so a missing table, an unreachable datastore or a ``NameError``
introduced by an edit produced a reassuring, entirely fictional all-clear — and produced it silently,
with nothing in the log to say a read had failed at all.

``readable()`` is the one place that decides what a failed read looks like. It follows the
convention these same routers already use for an unavailable *import* (``503`` +
"… unavailable", which the frontend renders as an error with a retry) and extends it to an
unavailable *query*.

Two deliberate choices:

* **The caller is told the type of failure, never its text.** A datastore error message carries file
  paths, table names and — on Postgres — connection details; those belong in the service log, not in
  a viewer's browser. The response names the surface and the exception class; the log line carries
  the rest.
* **``HTTPException`` passes through untouched.** A ``403`` raised by a capability check inside the
  block is an answer, not a failure, and must not be rewritten into a ``503``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_FAILURES: dict[str, int] = {}


@contextmanager
def readable(what: str) -> Iterator[None]:
    """Turn a failed read of *what* into a logged, counted ``503``.

    *what* is the surface as an operator would name it ("the EU-AI-Act register"), because it is
    read back in both the log line and the error the console shows.
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        _FAILURES[what] = _FAILURES.get(what, 0) + 1
        logger.warning(
            "could not read %s: %s: %s (failed %d time(s) in this process)",
            what,
            type(exc).__name__,
            exc,
            _FAILURES[what],
            exc_info=True,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{what} is unavailable ({type(exc).__name__}) — the console cannot confirm this "
            f"is empty, so it is not showing it as empty",
        ) from exc


def read_failures() -> dict[str, int]:
    """How many reads of each surface this process has failed to serve.

    Non-zero means the console refused to draw a panel. Unlike the empty list it used to draw, that
    refusal is visible here, in the log, and to the operator looking at the page.
    """
    return dict(_FAILURES)


def reset_read_failures() -> None:
    """Clear the counters (tests; a process-global map otherwise leaks state between them)."""
    _FAILURES.clear()
