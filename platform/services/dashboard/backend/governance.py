"""Governance & compliance aggregators (F14 / ADR 0063).

Honest, evidence-based governance views over the shipped compliance backend: EU-AI-Act risk classes
(`compliance_records`), model-card coverage (`model_cards`), an audit hash-chain integrity digest
(`audit_events`), and a derived NIST-AI-RMF posture. Every view reports **evidence coverage**, not
certification — gaps are surfaced as ``gap``/``partial``, never false green (F14 R1).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dbconn import connect


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _known_models(conn: sqlite3.Connection) -> set[str]:
    """Union of model names seen across the compliance-relevant tables (lowercased keys)."""
    names: set[str] = set()
    for table, col in (
        ("compliance_records", "model"),
        ("model_cards", "model"),
        ("model_costs", "model_name"),
    ):
        if _table_exists(conn, table):
            for r in conn.execute(f"SELECT DISTINCT {col} AS m FROM {table}"):
                if r["m"]:
                    names.add(str(r["m"]).lower())
    return names


# ── EU AI Act compliance (F14 R2) ─────────────────────────────────────────────


def compliance_status(db_path: str) -> dict[str, Any]:
    """Per-model risk class + technical-file (Annex IV) + provenance presence (F14 R2)."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "compliance_records"):
            return {"rows": [], "count": 0}
        rows = []
        for r in conn.execute(
            # The newest record per model. Selecting bare columns next to MAX() is a SQLite
            # extension — every other engine rejects it — so the latest row is picked by a
            # correlated subquery, which means the same thing on both backends.
            "SELECT model, version, risk_class, annex_iv_path, provenance_hash "
            "FROM compliance_records c "
            "WHERE version = (SELECT MAX(version) FROM compliance_records x "
            "                  WHERE x.model = c.model) "
            "ORDER BY model"
        ):
            rows.append(
                {
                    "model": r["model"],
                    "version": r["version"],
                    "riskClass": r["risk_class"] or "unclassified",
                    "technicalFile": bool(r["annex_iv_path"]),
                    "provenance": bool(r["provenance_hash"]),
                }
            )
        return {"rows": rows, "count": len(rows)}
    finally:
        conn.close()


# ── model-card coverage (F14 R5) ──────────────────────────────────────────────


def model_card_coverage(db_path: str) -> dict[str, Any]:
    """Which known models have a model card vs not, plus a coverage ratio (F14 R5)."""
    conn = _connect(db_path)
    try:
        models = _known_models(conn)
        carded: set[str] = set()
        if _table_exists(conn, "model_cards"):
            for r in conn.execute("SELECT DISTINCT model FROM model_cards"):
                if r["model"]:
                    carded.add(str(r["model"]).lower())
        with_card = sorted(models & carded)
        without_card = sorted(models - carded)
        total = len(models)
        return {
            "withCard": with_card,
            "withoutCard": without_card,
            "coverage": round(len(with_card) / total, 3) if total else None,
            "total": total,
        }
    finally:
        conn.close()


# ── audit hash-chain integrity (F14 R3) ───────────────────────────────────────


def audit_integrity(db_path: str, tail: int = 20) -> dict[str, Any]:
    """Report the audit log's **own** hash chain — the one `exa audit verify` checks (F14 R3).

    This used to compute a *different* chain. Its docstring said "``audit_events`` stores no
    per-row hash, so we compute a deterministic chain", and that was true when the page was
    written; the platform has stored `prev_hash` and `hash` on every event since. The page had
    become a parallel digest over five columns that no other tool could reproduce, so an operator
    copying `headDigest` as an external anchor was anchoring a number `exa audit verify` has never
    heard of — and `verified` was `True` *by construction*, which verifies nothing.

    It also read **every row of a table that grows forever** on each render, and built a dict per
    event to return the last twenty of them.

    What it reports now:

    - `headDigest` — the stored hash of the newest chained event: the real anchor, one indexed row.
    - `count` / `unchained` — how many events exist and how many carry no hash. Unchained rows are
      counted rather than hidden, because "we did not look at these" must never read as "these are
      fine"; every dashboard-written event was unchained once, and a verifier that skipped them
      still answered ok.
    - `verified` — whether the **last `tail` links** recompute, which is what a page can honestly
      check in constant time. `verifiedScope` says so in the payload, so a reader is never told the
      whole log was verified when it was not. Full verification is `exa audit verify`, which reads
      the log end to end and names the first broken link.
    """
    from examlops.data.audit import verify_tail

    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "audit_events"):
            return {
                "count": 0,
                "headDigest": None,
                "verified": True,
                "verifiedScope": "no audit log yet",
                "unchained": 0,
                "entries": [],
            }
        count = int(conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"])
        unchained = int(
            conn.execute("SELECT COUNT(*) AS c FROM audit_events WHERE hash IS NULL").fetchone()[
                "c"
            ]
        )
        rows = list(
            conn.execute(
                "SELECT id, source, actor, action, target, prev_hash, hash "
                "FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT ?",
                (tail,),
            )
        )
    finally:
        conn.close()

    rows.reverse()  # oldest first, the order the chain runs in
    head = rows[-1]["hash"] if rows else None
    ok, scope = verify_tail(tail)
    return {
        "count": count,
        "headDigest": head,
        "verified": ok,
        "verifiedScope": scope,
        "unchained": unchained,
        "entries": [
            {
                "seq": r["id"],
                "actor": r["actor"],
                "action": r["action"],
                "hash": r["hash"],
                "prevHash": r["prev_hash"],
            }
            for r in rows
        ],
    }


# ── NIST AI RMF posture (F14 R1 — honest evidence coverage) ────────────────────


def nist_posture(db_path: str) -> dict[str, Any]:
    """Derive a NIST-AI-RMF posture from available evidence — honest gaps, no false green (F14 R1).

    Each control's status reflects *evidence coverage* (satisfied / partial / gap), not certification:
    e.g. change-approval is ``satisfied`` only if approval audit events exist, model documentation is
    graded by model-card coverage.
    """
    conn = _connect(db_path)
    try:
        # Gather evidence signals.
        audit_n = _count(conn, "audit_events")
        approval_n = 0
        if _table_exists(conn, "audit_events"):
            approval_n = conn.execute(
                # Bound rather than inlined: harmless today (psycopg only reads `%` when
                # parameters are passed) and a trap the moment anyone adds one.
                "SELECT COUNT(*) AS c FROM audit_events WHERE action LIKE ?",
                ("%approv%",),
            ).fetchone()["c"]
        compliance_n = _count(conn, "compliance_records")
    finally:
        conn.close()

    cov = model_card_coverage(db_path)
    card_cov = cov["coverage"]

    controls = [
        _control(
            "MANAGE-4.1",
            "Manage",
            "Change approval logged",
            "satisfied" if approval_n else "gap",
            [f"{approval_n} approval audit events"] if approval_n else ["no approval events found"],
        ),
        _control(
            "MEASURE-2.1",
            "Measure",
            "Model documentation",
            _card_status(card_cov),
            [f"model-card coverage {int((card_cov or 0) * 100)}%"],
        ),
        _control(
            "MAP-1.1",
            "Map",
            "Risk classification (EU AI Act)",
            "satisfied" if compliance_n else "gap",
            [f"{compliance_n} compliance records"] if compliance_n else ["no risk classifications"],
        ),
        _control(
            "GOVERN-1.1",
            "Govern",
            "Audit trail present",
            "satisfied" if audit_n else "gap",
            [f"{audit_n} audit events"] if audit_n else ["no audit events"],
        ),
    ]
    satisfied = sum(1 for c in controls if c["status"] == "satisfied")
    return {"controls": controls, "satisfied": satisfied, "total": len(controls)}


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def _card_status(coverage: float | None) -> str:
    if coverage is None or coverage == 0:
        return "gap"
    if coverage >= 1.0:
        return "satisfied"
    return "partial"


def _control(
    control: str, function: str, title: str, status: str, evidence: list[str]
) -> dict[str, Any]:
    return {
        "control": control,
        "function": function,
        "title": title,
        "status": status,
        "evidence": evidence,
    }
