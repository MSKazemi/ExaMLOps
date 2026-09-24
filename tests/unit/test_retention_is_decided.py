"""Every append-only table has a retention answer, even if the answer is "we keep it forever".

`exa data retention-prune` prunes **two** tables: `drift_snapshots` and `input_snapshots`. The
command says so plainly, so nothing here is mis-documented. What is missing is the other half of the
sentence: the schema has **52** append-only event tables, and the rest grow for as long as the
platform runs. On a busy gateway `gateway_calls` gains a row per LLM request and `guardrail_events`
one per scan, forever — while the command's stated use case is "reclaim `platform.db` space".

Most of the unpruned ones *should* be unpruned. An audit chain that ages out is not tamper-evident,
a compliance record that disappears defeats the register it belongs to, and FinOps history is the
thing cost reporting is computed from. The problem is not that they are kept; it is that "kept on
purpose" and "nobody has looked" are indistinguishable when both are simply absent from a list.

So this guard makes the distinction explicit. Every append-only table must appear in exactly one
bucket, and a table added later belongs to none of them until somebody decides — which is the point.

**`GROWS_UNREVIEWED` is not a to-do list this file may quietly empty.** Adding a table to
`_PRUNABLE_TELEMETRY` starts deleting operators' data on the next scheduled run, and the trade — data
minimisation against keeping evidence — is a retention *policy* decision, not a code cleanup. The
list is here so the decision is visible, not so it can be made in passing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_DB = ROOT / "platform" / "cli" / "src" / "examlops" / "platform_db.py"

#: Kept deliberately, with the reason. These must never be added to the prune list.
RETAINED: dict[str, str] = {
    "audit_events": "the tamper-evident chain; ageing it out is what it exists to prevent",
    "audit_checkpoints": "the WORM anchors for that chain",
    "attestations": "supply-chain evidence, referenced by verification long after the fact",
    "compliance_records": "the EU-AI-Act register",
    "model_cards": "published model documentation",
    "data_versions": "dataset revision provenance a training run pins",
    "feature_versions": "feature provenance, same reason",
    "lineage_events": "what produced what — the question lineage answers is historical",
    "lineage_io": "the edges of that graph",
    "eval_runs": "evaluation evidence behind a promotion",
    "eval_suite_results": "carries the judge-calibration provenance (G7.3)",
    "gate_reports": "why a gate allowed or refused a release",
    "fairness_reports": "a published fairness assessment",
    "parity_checks": "evidence two implementations agreed",
    "data_quality_checks": "contract verdicts a dataset revision is judged by",
    "model_rollbacks": "what was rolled back and when",
    "carbon_records": "FinOps / Green-AI history, excluded by the command's own contract",
    "gpu_allocations": "cost attribution reads it",
    "inference_energy": "the measurement carbon accounting is computed from",
    "explanations": "a stored explanation is referenced by the decision it explains",
    "catalog_pulls": (
        "the only thread between a catalog entry and the model definition it became (ADR 0158) — "
        "pruning it would leave a pulled model with no answer to 'what did this start from, at "
        "which entry_hash?', the same provenance question data_versions and lineage_events are "
        "kept for. It is an operator action, not traffic: one row per `exa catalog pull`"
    ),
}

#: Pruned today by `_PRUNABLE_TELEMETRY`.
PRUNED: set[str] = {"drift_snapshots", "input_snapshots"}

#: Per-event tables that grow with traffic and have **no** retention decision recorded. Not a
#: backlog to clear in passing: see the module docstring. Sorted by how fast they grow.
GROWS_UNREVIEWED: dict[str, str] = {
    "gateway_calls": "one row per LLM request — the fastest-growing table on a busy gateway",
    "guardrail_events": "one per input/output scan, so roughly two per request",
    "cache_events": "one per semantic-cache lookup",
    "routing_events": "one per routing decision",
    "predictions": "one per served prediction",
    "live_metrics": "sampled serving metrics",
    "slo_samples": "SLI samples; the watermark means old ones are not re-read",
    "structured_output_events": "one per constrained decode",
    "reasoning_usage": "per-request reasoning-token accounting",
    "agent_tool_calls": "one per agent step",
    "explain_logs": "one per explanation request",
    "ab_assignments": "one per assigned request",
    "ab_results": "one per scored request",
    "shadow_results": "one per mirrored request",
    "challenger_samples": "one per challenger comparison sample",
    "fairness_samples": "one per sampled prediction",
    "ground_truth": "one per label supplied by the feedback loop",
    "label_queue": "items awaiting labelling",
    "drift_events": "drift status changes — slower, but unbounded",
    "scale_events": "autoscaler decisions",
    "canary_steps": "per canary step",
    "batch_jobs": "one per batch submission",
    "hpo_studies": "one per study",
    "hpo_trials": "one per trial, and a study has many",
    "model_optimizations": "one per quantise/optimise run",
    "perf_estimates": "one per estimate",
    "asset_materializations": "one per materialisation",
    "feature_materializations": "one per feature materialisation",
    "vector_items": "RAG chunks; bounded by corpus, unbounded by re-ingest",
    "vector_metrics": "per-index metric samples",
}


def _append_only_tables() -> set[str]:
    """Tables shaped like an event log: a surrogate autoincrement id and a `ts` column."""
    text = PLATFORM_DB.read_text(encoding="utf-8")
    found = set()
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\);", text, re.DOTALL):
        name, body = m.group(1), m.group(2)
        has_ts = re.search(
            r"^\s*ts\s+(DATETIME|TIMESTAMP|TEXT)", body, re.MULTILINE | re.IGNORECASE
        )
        surrogate = re.search(
            r"^\s*id\s+INTEGER PRIMARY KEY AUTOINCREMENT", body, re.MULTILINE | re.IGNORECASE
        )
        if has_ts and surrogate:
            found.add(name)
    assert len(found) > 40, f"only {len(found)} append-only tables parsed — the DDL's shape changed"
    return found


def _prunable() -> set[str]:
    text = PLATFORM_DB.read_text(encoding="utf-8")
    m = re.search(r"_PRUNABLE_TELEMETRY:\s*tuple\[str, \.\.\.\]\s*=\s*\(([^)]*)\)", text)
    assert m, "_PRUNABLE_TELEMETRY is no longer declared the way this guard reads it"
    return {t.strip().strip('"').strip("'") for t in m.group(1).split(",") if t.strip()}


def test_every_append_only_table_has_a_retention_decision():
    tables = _append_only_tables()
    classified = set(RETAINED) | PRUNED | set(GROWS_UNREVIEWED)
    undecided = sorted(tables - classified)
    assert not undecided, (
        "these append-only tables grow for as long as the platform runs and nobody has said what "
        f"should happen to them:\n  {undecided}\n"
        "Add each to RETAINED with the reason it must be kept, or to GROWS_UNREVIEWED if the "
        "retention question is open. Pruning it is a policy decision — see the module docstring."
    )


def test_the_classification_does_not_name_tables_that_are_gone():
    tables = _append_only_tables()
    stale = sorted((set(RETAINED) | set(GROWS_UNREVIEWED) | PRUNED) - tables)
    assert not stale, f"the classification names tables the schema no longer has: {stale}"


def test_the_prune_list_matches_what_this_guard_believes():
    """If `_PRUNABLE_TELEMETRY` changes, the reasoning here has to be revisited deliberately."""
    assert _prunable() == PRUNED, (
        f"`_PRUNABLE_TELEMETRY` is now {sorted(_prunable())} but this guard records "
        f"{sorted(PRUNED)}. Retention changed: move the table between the buckets above and say "
        "why in the changelog, because the next scheduled run will start deleting it."
    )


def test_nothing_retained_on_purpose_is_being_pruned():
    """The one outcome that would be a defect rather than a gap."""
    wrongly = sorted(_prunable() & set(RETAINED))
    assert not wrongly, (
        f"{wrongly} are pruned, and each is recorded as deliberately retained: {wrongly[0]} is "
        f"kept because {RETAINED[wrongly[0]]}"
    )
