"""Federated global-search aggregator (F2 / ADR 0056).

A single ``search()`` fans out across the platform's entities — models (registry), HPC jobs
(scheduler), audit events, and the static page/nav set — and returns **typed, grouped, ranked**
results, each linking to its F1 entity URL (F2 R3). It reuses the F9/F6 read layers
(:mod:`mlops`, :mod:`facility`) so there is one source of truth per entity kind.

Ranking is a small, dependency-free relevance score (:func:`score`) — exact match ranks above a
prefix, above a word-boundary hit, above a plain substring, above a fuzzy subsequence. Missing
tables degrade to no results for that source, never an error (graceful, per F6/F9).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import mlops

# Static navigation targets — always searchable, no DB needed (F1 pages).
_PAGES: list[tuple[str, str]] = [
    ("Overview", "/"),
    ("Services", "/services"),
    ("Models", "/models"),
    ("MLOps Console", "/mlops"),
    ("Facility Console", "/facility"),
    ("Datasets", "/datasets"),
    ("Pipelines", "/pipelines"),
    ("Drift", "/drift"),
    ("Approvals", "/approvals"),
    ("Audit", "/audit"),
    ("Config", "/config"),
    ("Docs", "/docs"),
]


# ── relevance scoring (F2 R3 — ranked) ────────────────────────────────────────


def score(query: str, text: str) -> int:
    """Dependency-free relevance score of ``text`` against ``query`` (0 = no match).

    exact 100 · prefix 80 · word-boundary 60 · substring 40 · fuzzy subsequence 20.
    """
    q = query.strip().lower()
    t = text.lower()
    if not q:
        return 0
    if t == q:
        return 100
    if t.startswith(q):
        return 80
    if any(word.startswith(q) for word in t.replace("/", " ").replace("-", " ").split()):
        return 60
    if q in t:
        return 40
    return 20 if _is_subsequence(q, t) else 0


def _is_subsequence(q: str, t: str) -> bool:
    it = iter(t)
    return all(ch in it for ch in q)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ── per-source providers ──────────────────────────────────────────────────────


def _search_pages(query: str) -> list[dict[str, Any]]:
    out = []
    for label, url in _PAGES:
        s = score(query, label)
        if s:
            out.append(
                {
                    "kind": "page",
                    "id": url,
                    "label": label,
                    "url": url,
                    "score": s,
                    "source": "docs",
                }
            )
    return out


def _search_models(db_path: str, query: str) -> list[dict[str, Any]]:
    out = []
    for row in mlops.registry_rows(db_path):
        s = max(score(query, row["name"]), score(query, row["mlflowName"]))
        if s:
            out.append(
                {
                    "kind": "model",
                    "id": row["mlflowName"],
                    "label": row["name"],
                    "url": f"/models/{row['mlflowName']}",
                    "score": s,
                    "source": "mlflow",
                }
            )
    return out


def _search_jobs(db_path: str, query: str) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "hpc_jobs"):
            return []
        out = []
        rows = conn.execute(
            "SELECT job_id, scheduler, model, state FROM hpc_jobs ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        for r in rows:
            s = max(score(query, r["job_id"]), score(query, r["model"]))
            if s:
                out.append(
                    {
                        "kind": "job",
                        "id": r["job_id"],
                        "label": f"{r['job_id']} · {r['model']} ({r['state']})",
                        "url": "/facility",
                        "score": s,
                        "source": "scheduler",
                    }
                )
        return out
    finally:
        conn.close()


def _search_audit(db_path: str, query: str) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "audit_events"):
            return []
        out = []
        rows = conn.execute(
            "SELECT action, target, source FROM audit_events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        seen: set[tuple] = set()
        for r in rows:
            label = f"{r['action']} {r['target']}".strip()
            key = (r["action"], r["target"])
            if key in seen:
                continue
            s = max(score(query, r["action"] or ""), score(query, r["target"] or ""))
            if s:
                seen.add(key)
                out.append(
                    {
                        "kind": "audit",
                        "id": label,
                        "label": label,
                        "url": "/audit",
                        "score": s,
                        "source": "audit",
                    }
                )
        return out
    finally:
        conn.close()


# ── federated search (F2 R3) ──────────────────────────────────────────────────


def search(db_path: str, query: str, limit: int = 20) -> dict[str, Any]:
    """Federated, grouped, ranked search across all entity sources.

    Returns ``{"query", "count", "groups": {source: [results…]}, "results": [flat ranked]}``.
    Empty/blank query returns no results (the UI shows recents instead).
    """
    if not query or not query.strip():
        return {"query": query, "count": 0, "groups": {}, "results": []}

    results = (
        _search_pages(query)
        + _search_models(db_path, query)
        + _search_jobs(db_path, query)
        + _search_audit(db_path, query)
    )
    results.sort(key=lambda r: (-r["score"], r["label"].lower()))
    results = results[:limit]

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(r["source"], []).append(r)

    return {"query": query, "count": len(results), "groups": groups, "results": results}
