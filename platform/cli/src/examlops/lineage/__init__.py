"""A2 — OpenLineage emission + provenance graph (ADR 0004).

``emit_lineage()`` builds an OpenLineage run event (with ExaMLOps ``examlops.`` facets),
POSTs it to Marquez when ``EXAMLOPS_OPENLINEAGE_URL`` is set, and **always** dual-writes
the corresponding ``platform_db`` rows so the graph and the operational DB cannot
diverge (R7). Emission is **fail-open** (R6): unset URL → no-op HTTP; set-but-unreachable
→ logged, never raises, never fails the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("examlops.lineage")

_PRODUCER = "https://github.com/seanergys/examlops"
_SCHEMA_URL = "https://openlineage.io/spec/1-0-5/OpenLineage.json"
_NS = "examlops"


@dataclass(frozen=True)
class Node:
    name: str  # bare name, e.g. "PM100Dataset" or "jpcp/18"
    type: str = "dataset"  # dataset | model | deployment

    @property
    def namespaced(self) -> str:
        return f"examlops://{self.type}/{self.name}"


def dataset_node(dataset: str, revision: str | None = None) -> Node:
    name = f"{dataset}@{revision}" if revision else dataset
    return Node(name=name, type="dataset")


def model_node(model: str, version: str | int) -> Node:
    return Node(name=f"{model}/{version}", type="model")


def deployment_node(name: str) -> Node:
    return Node(name=name, type="deployment")


# ── facet builders (R3, R12) ──────────────────────────────────────────────────


def _facet(fields: dict[str, Any]) -> dict[str, Any]:
    return {"_producer": _PRODUCER, "_schemaURL": _SCHEMA_URL, **fields}


def dataset_revision_facet(revision: str, kind: str = "content") -> dict[str, Any]:
    return {"examlops.dataset_revision": _facet({"revision": revision, "kind": kind})}


def cost_facet(gpu_hours: float, kwh: float = 0.0, co2e: float = 0.0) -> dict[str, Any]:
    return {"examlops.cost": _facet({"gpu_hours": gpu_hours, "kwh": kwh, "co2e_kg": co2e})}


def eval_facet(score: float, metric: str = "score") -> dict[str, Any]:
    return {"examlops.eval": _facet({"metric": metric, "score": score})}


def _openlineage_event(
    event_type: str,
    job: str,
    run_id: str,
    inputs: list[Node],
    outputs: list[Node],
    facets: dict[str, Any],
) -> dict[str, Any]:
    """Build an OpenLineage-schema-conformant run event (R11)."""
    now = datetime.now(UTC).isoformat()
    return {
        "eventType": event_type,
        "eventTime": now,
        "producer": _PRODUCER,
        "schemaURL": _SCHEMA_URL,
        "run": {"runId": run_id, "facets": facets},
        "job": {"namespace": _NS, "name": job},
        "inputs": [{"namespace": _NS, "name": n.namespaced} for n in inputs],
        "outputs": [{"namespace": _NS, "name": n.namespaced} for n in outputs],
    }


def _post(url: str, event: dict[str, Any]) -> None:  # pragma: no cover - network
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/lineage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=3)


def emit_lineage(
    event_type: str,
    job: str,
    run_id: str,
    inputs: list[Node] | None = None,
    outputs: list[Node] | None = None,
    facets: dict[str, Any] | None = None,
    *,
    dataset_revision: str | None = None,
    mlflow_run_id: str | None = None,
    model: str | None = None,
    model_version: str | int | None = None,
    trace_id: str | None = None,
) -> None:
    """Emit an OpenLineage event + dual-write platform_db. Fail-open (R6/R7)."""
    inputs = inputs or []
    outputs = outputs or []
    facets = dict(facets or {})
    if dataset_revision:
        facets.update(dataset_revision_facet(dataset_revision))

    # 1) Operational source of truth — always written (R7), independent of Marquez.
    try:
        from examlops.data.events import record_lineage_event

        record_lineage_event(
            run_id,
            job,
            event_type,
            inputs=[{"name": n.namespaced, "type": n.type} for n in inputs],
            outputs=[{"name": n.namespaced, "type": n.type} for n in outputs],
            dataset_revision=dataset_revision,
            mlflow_run_id=mlflow_run_id,
            model=model,
            model_version=str(model_version) if model_version is not None else None,
            trace_id=trace_id,
            facets=facets,
        )
    except Exception as exc:  # never fail the pipeline on a bookkeeping error
        logger.warning("lineage platform_db upsert failed: %s", exc)

    # 2) Best-effort push to Marquez (R6 fail-open).
    url = os.getenv("EXAMLOPS_OPENLINEAGE_URL")
    if not url:
        return
    try:
        event = _openlineage_event(event_type, job, run_id, inputs, outputs, facets)
        _post(url, event)
    except Exception as exc:
        logger.warning("OpenLineage emit to %s failed (ignored): %s", url, exc)


def build_event(
    event_type: str,
    job: str,
    run_id: str,
    inputs: list[Node] | None = None,
    outputs: list[Node] | None = None,
    facets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public helper to build (not send) an OpenLineage event — used in tests (R11)."""
    return _openlineage_event(
        event_type, job, run_id, inputs or [], outputs or [], dict(facets or {})
    )
