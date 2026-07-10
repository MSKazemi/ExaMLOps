"""SM3 — Skipper long-term memory admin: enumerate / export / erase / stats.

Run:  python -m skipper.memory_admin {stats|list|export|delete} [...]

Operates on ``AGENT_MEMORY_DB``. Enumeration/deletion do not need the embedding
backend (the store is opened without an index), so this works offline. Deletions
cascade to derived memories and are audited to ``platform_db.audit_events`` (ADR
0034). The immutable audit log is a separate store and is untouched by erasure.

This admin surface lives in the agent package (which owns the memory store), not in
the ``exa`` CLI, to keep the platform CLI free of a langgraph dependency.
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
    return p


def main(argv: list[str] | None = None, store=None) -> str:
    args = build_parser().parse_args(argv)
    out = _run(args, store if store is not None else open_store())
    print(out)
    return out


if __name__ == "__main__":  # pragma: no cover
    main()
