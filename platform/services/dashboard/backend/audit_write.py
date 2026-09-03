"""One writer for every dashboard audit event (D4, ADR 0028).

Sixteen routers and five modules each carried an identical `_audit()` that did a raw
``INSERT INTO audit_events``. That writes a row with no ``prev_hash``/``hash``, which puts it
**outside the hash chain** — and `verify_audit_chain` selects `WHERE hash IS NOT NULL`, so those
rows were not merely unverified, they were invisible to verification. `exa audit verify` answered
`ok: True` over a log from which every dashboard mutation — project deletion, connection deletion,
secret rotation, virtual-key issuance, the autopilot kill-switch — had been quietly excluded.

The append-only triggers still protected those rows from SQL-level edits, so nothing was
rewritable through the app. What was missing is what a chain is *for*: proof that no row was
inserted between others, reordered, or removed by someone with direct access to the database file.
D4 advertises "any edit/deletion/reordering breaks the chain", and for dashboard events that was
simply not true.

Routing through `examlops.data.audit.write_audit_event` — the writer the CLI already uses — puts
every dashboard event in the same chain as every CLI event, which is also the only way the two
surfaces can share one tamper-evident history rather than two half-histories.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def audit(
    actor: str,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
    *,
    source: str = "dashboard",
    tenant: str = "default",
    conn: Any = None,
) -> None:
    """Append a chained audit event for a dashboard action.

    ``source`` keeps the finer-grained origins some surfaces already record
    (``dashboard-copilot``, ``dashboard-flags``, …) rather than flattening them to one value —
    those distinctions are what let an operator tell a UI toggle from an agent proposal.

    ``conn`` is the caller's **open write transaction**, and the event is chained onto it. That is
    not an optimisation: every caller here has just written the thing it is auditing, SQLite admits
    one writer, and opening a second connection deadlocks until `busy_timeout` and fails. The first
    version of this module did exactly that and swallowed the error, turning a fix for *unchained*
    audit rows into *missing* ones — caught by ten existing tests that assert their action was
    recorded. Sharing the transaction also makes the audit atomic with the mutation: they commit
    together, so an action can no longer succeed while its record is lost.

    Falls back to a raw insert **only** when the shared package is unavailable (a deployment
    without `examlops` installed). That row is unchained, which is the situation this module exists
    to end — so it is logged rather than passed over, and `exa audit verify` counts it in
    `unchained`.
    """
    try:
        from examlops.data.audit import write_audit_event  # type: ignore

        write_audit_event(source, actor, action, target, details, tenant=tenant, conn=conn)
        return
    except ImportError:
        pass
    except Exception as exc:
        # Never silent: a dropped audit event is the failure this module was written to prevent.
        logger.error("AUDIT EVENT LOST for %s/%s: %s", action, target, exc)
        raise

    if conn is None:
        logger.error("audit event dropped (no examlops, no connection): %s/%s", action, target)
        return
    logger.warning(
        "writing UNCHAINED audit event %s/%s — examlops is not installed in this deployment",
        action,
        target,
    )
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        (source, actor, action, target, json.dumps(details or {})),
    )
