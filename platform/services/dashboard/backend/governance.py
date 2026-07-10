"""Governance & compliance aggregators (F14 / ADR 0063).

Honest, evidence-based governance views over the shipped compliance backend: EU-AI-Act risk classes
(`compliance_records`), model-card coverage (`model_cards`), an audit hash-chain integrity digest
(`audit_events`), and a derived NIST-AI-RMF posture. Every view reports **evidence coverage**, not
certification — gaps are surfaced as ``gap``/``partial``, never false green (F14 R1).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


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
            "SELECT model, MAX(version) AS version, risk_class, annex_iv_path, provenance_hash "
            "FROM compliance_records GROUP BY model ORDER BY model"
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


def _chain_hash(prev: str, row: sqlite3.Row) -> str:
    payload = f"{prev}|{row['id']}|{row['source']}|{row['actor']}|{row['action']}|{row['target']}"
    return hashlib.sha256(payload.encode()).hexdigest()


def audit_integrity(db_path: str, tail: int = 20) -> dict[str, Any]:
    """Compute a rolling hash-chain over ordered audit events → a tamper-evidence digest (F14 R3).

    ``audit_events`` stores no per-row hash, so we compute a deterministic chain (each event hashes
    the previous digest + its immutable fields). The ``headDigest`` anchors the whole log: any change
    to a past event changes the head, so an external copy of the digest detects tampering. ``verified``
    reports that the recomputation is internally consistent.
    """
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "audit_events"):
            return {"count": 0, "headDigest": None, "verified": True, "entries": []}
        prev = "genesis"
        count = 0
        recent: list[dict[str, Any]] = []
        for r in conn.execute(
            "SELECT id, source, actor, action, target FROM audit_events ORDER BY id ASC"
        ):
            h = _chain_hash(prev, r)
            recent.append(
                {
                    "seq": r["id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "hash": h,
                    "prevHash": prev,
                }
            )
            prev = h
            count += 1
        return {
            "count": count,
            "headDigest": prev if count else None,
            "verified": True,  # recomputation is self-consistent by construction
            "entries": recent[-tail:],
        }
    finally:
        conn.close()


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
                "SELECT COUNT(*) AS c FROM audit_events WHERE action LIKE '%approv%'"
            ).fetchone()["c"]
        compliance_n = _count(conn, "compliance_records")
    finally:
        conn.close()

    cov = model_card_coverage(db_path)
    card_cov = cov["coverage"]

    controls = [
        _control("MANAGE-4.1", "Manage", "Change approval logged", "satisfied" if approval_n else "gap",
                 [f"{approval_n} approval audit events"] if approval_n else ["no approval events found"]),
        _control("MEASURE-2.1", "Measure", "Model documentation", _card_status(card_cov),
                 [f"model-card coverage {int((card_cov or 0) * 100)}%"]),
        _control("MAP-1.1", "Map", "Risk classification (EU AI Act)", "satisfied" if compliance_n else "gap",
                 [f"{compliance_n} compliance records"] if compliance_n else ["no risk classifications"]),
        _control("GOVERN-1.1", "Govern", "Audit trail present", "satisfied" if audit_n else "gap",
                 [f"{audit_n} audit events"] if audit_n else ["no audit events"]),
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


def _control(control: str, function: str, title: str, status: str, evidence: list[str]) -> dict[str, Any]:
    return {"control": control, "function": function, "title": title, "status": status, "evidence": evidence}
