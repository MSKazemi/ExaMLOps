"""Evidence sufficiency for compliance packs (ADR 0110 decision 6).

A collector answers "is there evidence?" — it counts rows. That is not the question an auditor
asks. The auditor asks "can this evidence be relied on?", and a pack that prints *"Change log:
412 audited changes ✓"* over an audit chain that no longer verifies is the confident partial pack
ADR 0110 forbids: it looks complete and is not.

So every section of a pack is judged on two axes: whether evidence exists (the collector), and
whether the records it comes from are **tamper-evident and intact**. The platform can vouch for a
record in exactly two ways (ADR 0110 decisions 1–2):

- it is an event in the **hash-chained audit log** (`audit_events`), verified link by link; or
- it is a row in an **anchored side table** (`examlops.telemetry_anchor.ANCHORED_TABLES`), whose
  ranges are hashed into the chain and re-verified.

Every other table is outside both, and the pack says so rather than implying otherwise.

Four statuses, from the auditor's side:

========== =====================================================================================
verified   evidence exists and every source it draws on was checked and passed
unverified evidence exists, but at least one source is outside the chain and the anchors — it is
           *named*, not counted as a gap (otherwise no pack could ever be complete)
insufficient evidence exists, but verification **failed** or **could not run**: a broken chain
           link, a broken anchor, or an autonomous action with no declared inverse. This is a
           gap — a declaration resting on it stays a draft
missing    no evidence found
========== =====================================================================================

"Could not run" is insufficient, never verified: a check that raised is the absence of a check,
and treating it as a pass is the failure this module exists to prevent (the same rule as
``verify_audit_chain`` counting unchained rows instead of skipping them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VERIFIED = "verified"
UNVERIFIED = "unverified"
INSUFFICIENT = "insufficient"
MISSING = "missing"
STATUSES = (VERIFIED, UNVERIFIED, INSUFFICIENT, MISSING)

#: Which tables each evidence collector (``examlops.compliance._COLLECTORS``) reads. Kept next to
#: the statuses so a new collector without an entry is caught by the test that checks the two
#: mappings have the same keys — an unmapped section would otherwise read as "verified" by
#: default, the exact confident-partial-pack failure.
EVIDENCE_SOURCES: dict[str, tuple[str, ...]] = {
    "system_description": ("compliance_systems",),
    "development_process": ("lineage_events",),
    "data_governance": ("dataset_revisions", "data_quality_checks"),
    "performance": ("eval_suite_results",),
    "fairness": ("fairness_samples",),
    "risk_management": ("guardrail_events", "drift_events"),
    "integrity": ("model_boms",),
    "monitoring": ("drift_events", "slo_specs"),
    "record_keeping": ("audit_events",),
    "changes": ("audit_events",),
}

_CHAIN = "audit_events"


@dataclass
class IntegrityState:
    """What the chain and the anchors can vouch for, computed once per pack."""

    chain: dict[str, Any] | None = None
    chain_error: str | None = None
    anchors: dict[str, Any] | None = None
    anchors_error: str | None = None
    anchored_tables: tuple[str, ...] = ()
    autonomous_without_rollback: int | None = None
    autonomy_error: str | None = None

    # ── summary ─────────────────────────────────────────────────────────────

    @property
    def chain_ok(self) -> bool:
        return bool(self.chain and self.chain.get("ok"))

    def chain_summary(self) -> str:
        if self.chain is None:
            return f"not verified — the check failed: {self.chain_error}"
        c = self.chain
        if not c.get("ok"):
            return (
                f"BROKEN at event {c.get('broken_at_id')} ({c.get('reason')}); "
                f"{c.get('count', 0)} chained event(s)"
            )
        head = str(c.get("head_hash") or "")[:16]
        extra = f"; {c['unchained']} event(s) outside the chain" if c.get("unchained") else ""
        return f"intact — {c.get('count', 0)} event(s), head `{head}…`{extra}"

    def anchors_summary(self) -> str:
        if self.anchors is None:
            return f"not verified — the check failed: {self.anchors_error}"
        a = self.anchors
        parts = [f"{a.get('anchors_checked', 0)} anchor(s) checked"]
        if a.get("breaks"):
            parts.append(f"{len(a['breaks'])} BROKEN")
        if a.get("pruned_anchors"):
            parts.append(f"{len(a['pruned_anchors'])} covering audited retention prunes")
        unanchored = a.get("unanchored_rows") or {}
        if unanchored:
            parts.append(
                "unanchored rows: " + ", ".join(f"{t} {n}" for t, n in sorted(unanchored.items()))
            )
        return "; ".join(parts)

    # ── per-source judgement ────────────────────────────────────────────────

    def judge_source(self, table: str) -> tuple[str, str | None]:
        """``(status, reason)`` for one source table: verified / unverified / insufficient."""
        if table == _CHAIN:
            if self.chain is None:
                return INSUFFICIENT, f"audit chain could not be verified ({self.chain_error})"
            if not self.chain.get("ok"):
                return INSUFFICIENT, (
                    f"audit chain is broken at event {self.chain.get('broken_at_id')} "
                    f"({self.chain.get('reason')}) — events after it cannot be relied on"
                )
            if self.chain.get("unchained"):
                return UNVERIFIED, (
                    f"{self.chain['unchained']} audit event(s) carry no hash and sit outside the "
                    "chain"
                )
            return VERIFIED, None
        if table in self.anchored_tables:
            if self.anchors is None:
                return (
                    INSUFFICIENT,
                    f"anchors of {table} could not be verified ({self.anchors_error})",
                )
            broken = [b for b in self.anchors.get("breaks") or [] if b.get("table") == table]
            if broken:
                b = broken[0]
                return INSUFFICIENT, (
                    f"{table}: anchor (event {b.get('event_id')}, rows {b.get('from_id')}–"
                    f"{b.get('to_id')}) no longer matches — {b.get('reason')}"
                )
            # An anchor can only vouch for rows through the chain, so a break there matters too.
            if self.chain is None or not self.chain.get("ok"):
                return (
                    INSUFFICIENT,
                    f"{table}: its anchors live in an audit chain that does not verify",
                )
            pending = int((self.anchors.get("unanchored_rows") or {}).get(table, 0))
            if pending:
                return UNVERIFIED, (
                    f"{table}: {pending} row(s) newer than the last anchor (run: exa audit anchor)"
                )
            return VERIFIED, None
        return UNVERIFIED, f"{table} is outside the hash chain and the telemetry anchors"


def integrity_state() -> IntegrityState:
    """Verify the chain, the anchors and the autonomy record — each independently, fail-closed.

    A failure in one check does not stop the others, and a check that raises is recorded as
    *could not verify*, which :meth:`IntegrityState.judge_source` treats as insufficient.
    """
    state = IntegrityState()
    try:
        from examlops.data.audit import verify_audit_chain

        state.chain = verify_audit_chain()
    except Exception as exc:  # noqa: BLE001 - recorded, and judged insufficient
        state.chain_error = f"{type(exc).__name__}: {exc}"
    try:
        from examlops.telemetry_anchor import ANCHORED_TABLES, verify_anchors

        state.anchored_tables = tuple(ANCHORED_TABLES)
        state.anchors = verify_anchors()
    except Exception as exc:  # noqa: BLE001
        state.anchors_error = f"{type(exc).__name__}: {exc}"
    try:
        from examlops.data.audit import count_autonomous_without_rollback

        # Counted by the database, not by adding up a page of rows: this number decides whether
        # `record_keeping` is insufficient, so a violation that fell off the end of a listing
        # would be a compliance pack vouching for a record that contains it.
        state.autonomous_without_rollback = count_autonomous_without_rollback(since_days=3650)
    except Exception as exc:  # noqa: BLE001
        state.autonomy_error = f"{type(exc).__name__}: {exc}"
    return state


@dataclass
class Assessment:
    status: str
    reasons: list[str] = field(default_factory=list)


def assess_section(control: str, present: bool, state: IntegrityState) -> Assessment:
    """Judge one pack section. ``present`` is the collector's answer."""
    if not present:
        return Assessment(MISSING)
    sources = EVIDENCE_SOURCES.get(control)
    if not sources:
        # An unmapped section has unknown provenance. Refusing to call it verified is the point.
        return Assessment(UNVERIFIED, [f"no evidence-source mapping for section '{control}'"])
    statuses: list[str] = []
    reasons: list[str] = []
    for table in sources:
        status, reason = state.judge_source(table)
        statuses.append(status)
        if reason:
            reasons.append(reason)
    if control == "record_keeping":
        # ADR 0110 decision 4: an autonomous action with no declared inverse is a violation the
        # record must surface — it is precisely the entry an auditor would ask about.
        if state.autonomous_without_rollback is None:
            statuses.append(INSUFFICIENT)
            reasons.append(f"autonomy record could not be checked ({state.autonomy_error})")
        elif state.autonomous_without_rollback:
            statuses.append(INSUFFICIENT)
            reasons.append(
                f"{state.autonomous_without_rollback} autonomous action(s) declared no "
                "rollback_ref (ADR 0110 decision 4) — see: exa audit autonomy"
            )
    if INSUFFICIENT in statuses:
        return Assessment(INSUFFICIENT, reasons)
    if UNVERIFIED in statuses:
        return Assessment(UNVERIFIED, reasons)
    return Assessment(VERIFIED, reasons)
