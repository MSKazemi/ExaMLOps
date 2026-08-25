"""SM3 — local/offline Skipper memory recovery and administration.

Run:  python -m skipper.memory_admin {stats|list|export|delete} [...]

This is the explicit local compatibility path behind ``exa agent memory ... --local``.
Normal administration uses the authenticated agent HTTP API so the server can enforce
principal and tenant ownership. This module operates directly on ``AGENT_MEMORY_DB``;
enumeration/deletion do not need the embedding
backend (the store is opened without an index), so this works offline. Deletions
cascade to derived memories and are audited to ``platform_db.audit_events`` (ADR
0034). The immutable audit log is a separate store and is untouched by erasure.

Because direct file access has no authenticated request identity, do not use it as a
multi-user remote administration surface.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from skipper import config, memory_types


def open_store(db_path: str | None = None):
    """Open the memory store WITHOUT a vector index (enough for list/get/delete)."""
    from langgraph.store.sqlite import SqliteStore

    conn = sqlite3.connect(db_path or config.AGENT_MEMORY_DB, check_same_thread=False)
    conn.isolation_level = None  # autocommit — SqliteStore manages its own transactions
    store = SqliteStore(conn)
    store.setup()
    return store


def _run(args: argparse.Namespace, store) -> str:
    if args.cmd == "stats":
        return json.dumps(memory_types.stats(store), indent=2)
    if args.cmd == "list":
        items = memory_types.list_kind(store, args.kind, scope=args.scope, limit=args.limit)
        return json.dumps(
            [{"key": it.key, "text": it.value.get("text", "")} for it in items], indent=2
        )
    if args.cmd == "export":
        return json.dumps(memory_types.export_all(store), indent=2, default=str)
    if args.cmd == "delete":
        n = memory_types.erase(store, args.kind, scope=args.scope, operator=args.operator)
        return f"Deleted {n} {args.kind} memory item(s)" + (
            f" for {args.scope}" if args.scope else ""
        )
    if args.cmd == "review":
        from skipper import memory_review

        if args.review_cmd == "list":
            return json.dumps(memory_review.list_pending(), indent=2, default=str)
        if args.review_cmd == "approve":
            return memory_review.approve(args.id, store, reviewer=args.operator)
        if args.review_cmd == "reject":
            return memory_review.reject(args.id, reviewer=args.operator, reason=args.reason)
        raise ValueError(f"unknown review sub-command {args.review_cmd!r}")
    raise ValueError(f"unknown command {args.cmd!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skipper.memory_admin", description="Skipper long-term memory admin"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="Count memories per kind")
    lp = sub.add_parser("list", help="List memories of a kind")
    lp.add_argument("kind", choices=memory_types.KINDS)
    lp.add_argument("--scope", default=None, help="Task-class / model / operator scope")
    lp.add_argument("--limit", type=int, default=50)
    sub.add_parser("export", help="Export all memories as JSON (GDPR export)")
    dp = sub.add_parser("delete", help="Delete memories of a kind (cascade + audited)")
    dp.add_argument("kind", choices=memory_types.KINDS)
    dp.add_argument("--scope", default=None, help="Limit deletion to a scope (e.g. an operator)")
    dp.add_argument(
        "--operator", default=config.AGENT_ACTOR, help="Actor recorded in the audit log"
    )
    # SM3 review-queue (BL-009): batch review of queued procedure writes.
    rp = sub.add_parser("review", help="Review queued procedure writes (list/approve/reject)")
    rsub = rp.add_subparsers(dest="review_cmd", required=True)
    rsub.add_parser("list", help="List pending procedure reviews")
    ap = rsub.add_parser("approve", help="Approve a review (commit to memory)")
    ap.add_argument("id", type=int)
    ap.add_argument("--operator", default=config.AGENT_ACTOR, help="Reviewer name")
    jp = rsub.add_parser("reject", help="Reject a review (drop it)")
    jp.add_argument("id", type=int)
    jp.add_argument("--operator", default=config.AGENT_ACTOR, help="Reviewer name")
    jp.add_argument("--reason", default="", help="Why it was rejected")
    return p


def main(argv: list[str] | None = None, store=None) -> str:
    args = build_parser().parse_args(argv)
    owns_store = store is None
    store = store if store is not None else open_store()
    try:
        out = _run(args, store)
    finally:
        # Close the SQLite connection we opened (a caller-injected store is theirs).
        conn = getattr(store, "conn", None)
        if owns_store and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    print(out)
    return out


if __name__ == "__main__":  # pragma: no cover
    main()
