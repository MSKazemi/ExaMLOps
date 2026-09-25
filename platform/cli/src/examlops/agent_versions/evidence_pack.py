"""The per-version evidence pack (ADR 0146 decision 6).

One JSON document per agent version that answers the transparency questions an auditor asks -
*what exactly is this agent, how was it evaluated, by which judges, what may it do, and who moved
it into and out of service* - assembled from records the platform already keeps:

* the content-addressed tuple and whether its signature verifies (``agent_versions``);
* every evaluation result for the version, with Wilson intervals and the ``calibration_id`` of
  the judge that produced it (``eval_suite_results``), and each calibration record it names
  (``judge_calibrations``) - ADR 0111's provenance;
* the gate reports recorded for it (``gate_reports``);
* the tool grants stored for the version and for its agent name (ADR 0145), and its contract;
* every alias move that set, replaced or restored it, with the evidence stored on the move
  (``agent_alias_history``) - promotions and rollbacks;
* the audit events naming it, read from the hash-chained log (ADR 0110), with the chain head so
  a reader can check the events against the live chain.

The pack carries its own ``sha256`` digest over the canonical JSON of everything else, so a
copy handed to a regulator can be checked for alteration. It is read-only: exporting writes no
row. Serves EU AI Act Art. 50 transparency (in force since 2 Aug 2026).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

__all__ = ["PACK_SCHEMA", "export_evidence_pack", "pack_digest"]

PACK_SCHEMA = "examlops-agent-evidence-pack/v1"
_MAX_EVENTS = 1000


def pack_digest(pack: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the pack without its ``digest`` field."""
    body = {k: v for k, v in pack.items() if k != "digest"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _loads(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def export_evidence_pack(ref: str, *, tenant: str | None = None) -> dict[str, Any]:
    """Assemble the evidence pack of the version ``ref`` (id or ``<agent>@<alias>``).

    Raises ``LookupError`` when the version is unknown. ``tenant`` narrows the audit events to
    one tenant (applied in SQL before the event cap).
    """
    from examlops.agent_versions.service import get, model_key, verify_signature
    from examlops.data import agent_versions as store
    from examlops.data.audit import audit_chain_head
    from examlops.data.evaluation import (
        get_calibration_by_id,
        get_eval_results_for_agent_version,
    )
    from examlops.tool_broker.service import list_grants

    row = get(ref)
    if row is None:
        raise LookupError(f"unknown agent version {ref!r}")
    vid, agent, manifest = row["version_id"], row["agent"], row["manifest"]
    key = model_key(agent)

    evaluations = [
        {
            k: r.get(k)
            for k in (
                "suite",
                "metric",
                "score",
                "score_lo",
                "score_hi",
                "sample_size",
                "judge_model",
                "judge_prompt_version",
                "calibration_id",
                "dataset_revision",
                "run_id",
                "agent_version_id",
                "ts",
            )
        }
        for r in get_eval_results_for_agent_version(agent, vid, limit=_MAX_EVENTS)
    ]
    calibrations = []
    for cid in sorted({str(e["calibration_id"]) for e in evaluations if e.get("calibration_id")}):
        cal = get_calibration_by_id(cid)
        calibrations.append({"calibration_id": cid, "record": cal, "found": cal is not None})
    gate_reports = [
        {**g, "report": _loads(g.pop("report_json", None))}
        for g in store.gate_reports_for(key, vid, limit=_MAX_EVENTS)
    ]
    grants = [g for g in list_grants() if g["subject"] in (vid, agent)]
    moves = [
        {**m, "evidence": _loads(m.pop("evidence_json", None))}
        for m in store.history_for_version(agent, vid)
    ]
    events = store.audit_for_version(agent, vid, tenant=tenant, limit=_MAX_EVENTS)
    for e in events:
        e["details"] = _loads(e.get("details"))
    aliases = sorted(a["alias"] for a in store.list_aliases(agent) if a["version_id"] == vid)
    pack: dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "version": {
            "version_id": vid,
            "agent": agent,
            "signed": bool(row.get("signature")),
            "signature_valid": verify_signature(row) if row.get("signature") else None,
            "registered_at": row.get("created_at"),
            "registered_by": row.get("actor"),
            "manifest": manifest,
        },
        "current_aliases": aliases,
        "contract": manifest.get("policy", {}),
        "grants": grants,
        "evaluation": {
            "model_key": key,
            "declared_suites": (manifest.get("eval") or {}).get("suites", []),
            "non_inferiority_margin": (manifest.get("eval") or {}).get("non_inferiority_margin"),
            "results": evaluations,
            "calibrations": calibrations,
            "gate_reports": gate_reports,
        },
        "promotions_and_rollbacks": moves,
        "audit": {
            "events": events,
            "truncated": len(events) >= _MAX_EVENTS,
            "chain_head": audit_chain_head(),
        },
    }
    pack["digest"] = pack_digest(pack)
    return pack
