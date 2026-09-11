"""A2 — OpenLineage emission + provenance graph (ADR 0004).

``emit_lineage()`` builds an OpenLineage run event (with ExaMLOps ``examlops.`` facets),
POSTs it to Marquez when ``EXAMLOPS_OPENLINEAGE_URL`` is set, and **always** dual-writes
the corresponding ``platform_db`` rows so the graph and the operational DB cannot
diverge (R7). Emission is **fail-open** (R6): unset URL → no-op HTTP; set-but-unreachable
→ logged, never raises, never fails the pipeline.

The event is shaped for the OpenLineage 2-0-2 schema, which the platform's own ids do not fit
as they are, so the *event* — not the ``platform_db`` rows — translates three things:

- ``run.runId`` must be a UUID. An MLflow run id already is one (32 hex digits) and is sent as
  such; any other id (``train-jpcp-FData``) becomes a name-based UUID, the same for the run's
  START and COMPLETE, and the original rides along in the ``examlops.run`` facet.
- A dataset revision is a **version of one dataset**, not a new dataset: the event names
  ``examlops://dataset/FData`` and carries the revision in the standard ``version`` facet, so a
  receiver draws one node per dataset with its versions rather than one node per revision.
- A facet must be an object carrying ``_producer`` and ``_schemaURL``. A caller's free-form
  values (``{"backend": "minio"}``) are gathered into the ``examlops.run`` facet instead of being
  sent as bare values.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("examlops.lineage")

_PRODUCER = "https://github.com/MSKazemi/ExaMLOps"
_SPEC = "https://openlineage.io/spec/2-0-2/OpenLineage.json"
_SCHEMA_URL = f"{_SPEC}#/$defs/RunEvent"
#: A custom run facet conforms to the base ``RunFacet`` — the truthful schema to point at, since
#: the ``examlops.*`` facets have no schema document of their own.
_RUN_FACET_SCHEMA = f"{_SPEC}#/$defs/RunFacet"
_VERSION_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json"
    "#/$defs/DatasetVersionDatasetFacet"
)
_ERROR_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"
)
_PARENT_FACET_SCHEMA = (
    "https://openlineage.io/spec/facets/1-1-0/ParentRunFacet.json#/$defs/ParentRunFacet"
)
#: The most of a failure message an event carries. It goes to whatever receiver is configured, so
#: it is the reason, not a log.
_ERROR_MESSAGE_MAX = 500
_NS = "examlops"
#: Name-based UUIDs for run ids that are not UUIDs. Fixed forever: changing it would give every
#: past run a new id in the receiver.
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, f"{_PRODUCER}/openlineage/run")


@dataclass(frozen=True)
class Node:
    name: str  # bare name, e.g. "PM100Dataset" or "jpcp/18"
    type: str = "dataset"  # dataset | model | deployment
    #: A dataset revision: part of ``name`` for platform_db, a version facet in the event.
    version: str | None = None

    @property
    def namespaced(self) -> str:
        return f"examlops://{self.type}/{self.name}"

    @property
    def lineage_name(self) -> str:
        """The OpenLineage dataset name: stable across versions."""
        if self.version is not None and self.name.endswith(f"@{self.version}"):
            return f"examlops://{self.type}/{self.name[: -len(self.version) - 1]}"
        return self.namespaced


def dataset_node(dataset: str, revision: str | None = None) -> Node:
    name = f"{dataset}@{revision}" if revision else dataset
    return Node(name=name, type="dataset", version=revision or None)


def model_node(model: str, version: str | int) -> Node:
    return Node(name=f"{model}/{version}", type="model")


def deployment_node(name: str) -> Node:
    return Node(name=name, type="deployment")


def prompt_node(name: str, version: str | int) -> Node:
    """A prompt version as an A2 node (ADR 0009 clause 5).

    Version-scoped like a model version, because that is what a prompt version is: an immutable
    artifact a label points at. The **label** is the deployment node it feeds.
    """
    return Node(name=f"{name}/v{version}", type="prompt")


# ── facet builders (R3, R12) ──────────────────────────────────────────────────


def _facet(fields: dict[str, Any]) -> dict[str, Any]:
    return {"_producer": _PRODUCER, "_schemaURL": _RUN_FACET_SCHEMA, **fields}


def dataset_revision_facet(revision: str, kind: str = "content") -> dict[str, Any]:
    return {"examlops.dataset_revision": _facet({"revision": revision, "kind": kind})}


def cost_facet(
    gpu_hours: float, kwh: float = 0.0, co2e: float = 0.0, *, cost_usd: float | None = None
) -> dict[str, Any]:
    fields: dict[str, Any] = {"gpu_hours": gpu_hours, "kwh": kwh, "co2e_kg": co2e}
    if cost_usd is not None:
        fields["cost_usd"] = cost_usd
    return {"examlops.cost": _facet(fields)}


def hpc_job_id_facet(job_id: str, scheduler: str | None = None) -> dict[str, Any]:
    """Which scheduler job produced this run (clause 2).

    Scheduler-neutral by design: the same field carries a Slurm job id, a Flux jobid or a mock
    one, because a lineage graph that only understands Slurm cannot describe a Flux site — and
    ADR 0023's scheduler abstraction exists precisely so nothing above it has to care.

    Namespaced under its own key like every sibling facet. A flat return would merge
    ``_producer``/``_schemaURL`` into the top-level facets dict and collide with whichever other
    facet was attached to the same event.
    """
    return {"examlops.hpc_job": _facet({"job_id": job_id, "scheduler": scheduler or ""})}


def eval_facet(score: float, metric: str = "score") -> dict[str, Any]:
    return {"examlops.eval": _facet({"metric": metric, "score": score})}


def mlflow_run_facet(run_id: str) -> dict[str, Any]:
    """The MLflow run behind this lineage run (clause 2) — so a receiver can link to it."""
    return {"examlops.mlflow_run": _facet({"run_id": run_id})}


def error_facet(message: str) -> dict[str, Any]:
    """Why a run failed, as the standard OpenLineage ``errorMessage`` run facet (clause 1's
    ``FAIL``). Truncated, and without a stack trace: a trace carries local paths to a receiver
    that may sit outside the platform, and the message is what an operator needs there."""
    text = (
        message if len(message) <= _ERROR_MESSAGE_MAX else message[: _ERROR_MESSAGE_MAX - 1] + "…"
    )
    return {
        "errorMessage": {
            "_producer": _PRODUCER,
            "_schemaURL": _ERROR_FACET_SCHEMA,
            "message": text,
            "programmingLanguage": "python",
        }
    }


def parent_facet(run_id: str, job: str) -> dict[str, Any]:
    """The standard OpenLineage ``parent`` run facet: this run was started for ``run_id`` of
    ``job``. The parent's id is translated exactly as its own events were, so it resolves."""
    return {
        "parent": {
            "_producer": _PRODUCER,
            "_schemaURL": _PARENT_FACET_SCHEMA,
            "run": {"runId": lineage_run_id(run_id)},
            "job": {"namespace": _NS, "name": job},
        }
    }


def scheduled_run_facet(flow_run_id: str) -> dict[str, Any]:
    """The training run a request scheduled: its Prefect flow run, and the same run's lineage id.

    A request (``exa retrain``) and the flow it starts are two runs of two jobs; this is the link
    from the first to the second. The flow's own events use the flow run id as their run id, so a
    receiver can follow it straight to the training run and to Prefect.
    """
    return {
        "examlops.scheduled_run": _facet(
            {"flow_run_id": flow_run_id, "run_id": lineage_run_id(flow_run_id)}
        )
    }


def trace_facet(trace_id: str) -> dict[str, Any]:
    """The OTel trace this run belongs to (clause 5): lineage and traces are correlated, not merged."""
    return {"examlops.trace": _facet({"trace_id": trace_id})}


def _openlineage_event(
    event_type: str,
    job: str,
    run_id: str,
    inputs: list[Node],
    outputs: list[Node],
    facets: dict[str, Any],
) -> dict[str, Any]:
    """Build an OpenLineage 2-0-2 run event (R11) — see the module docstring for what it
    translates and why."""
    now = datetime.now(UTC).isoformat()
    run_uuid = lineage_run_id(run_id)
    run_facets: dict[str, Any] = {}
    loose: dict[str, Any] = {}
    for key, value in facets.items():
        if isinstance(value, dict) and "_producer" in value and "_schemaURL" in value:
            run_facets[key] = value
        else:
            loose[key] = value
    if loose or run_uuid != run_id:
        run_facets["examlops.run"] = _facet({"run_id": run_id, **loose})
    return {
        "eventType": event_type,
        "eventTime": now,
        "producer": _PRODUCER,
        "schemaURL": _SCHEMA_URL,
        "run": {"runId": run_uuid, "facets": run_facets},
        "job": {"namespace": _NS, "name": job},
        "inputs": [_dataset(n) for n in inputs],
        "outputs": [_dataset(n) for n in outputs],
    }


def lineage_run_id(run_id: str) -> str:
    """``run_id`` as the UUID an OpenLineage event requires.

    A UUID in any accepted spelling — an MLflow run id is 32 hex digits — is sent canonically, so
    the receiver's run and the MLflow run share an id. Anything else maps to a name-based UUID:
    deterministic, so a run's START and COMPLETE events land on one run.
    """
    try:
        return str(uuid.UUID(run_id))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(_RUN_ID_NAMESPACE, str(run_id)))


def _dataset(node: Node) -> dict[str, Any]:
    entry: dict[str, Any] = {"namespace": _NS, "name": node.lineage_name}
    if node.version is not None:
        entry["facets"] = {
            "version": {
                "_producer": _PRODUCER,
                "_schemaURL": _VERSION_FACET_SCHEMA,
                "datasetVersion": node.version,
            }
        }
    return entry


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
    hpc_job_id: str | None = None,
    scheduler: str | None = None,
) -> None:
    """Emit an OpenLineage event + dual-write platform_db. Fail-open (R6/R7)."""
    inputs = inputs or []
    outputs = outputs or []
    facets = dict(facets or {})
    if dataset_revision:
        facets.update(dataset_revision_facet(dataset_revision))
    if hpc_job_id:
        facets.update(hpc_job_id_facet(hpc_job_id, scheduler))
    # Clause 5 correlates lineage with traces, and no path passed a trace id: take it from the
    # active span, so every path is correlated whenever tracing is on — and none is when it is off.
    if trace_id is None:
        trace_id = current_trace_id()
    # Kept in platform_db's own columns as well; in the event they were missing, so a receiver
    # showed a run it could not link back to MLflow or to its trace.
    if mlflow_run_id:
        facets.update(mlflow_run_facet(mlflow_run_id))
    if trace_id:
        facets.update(trace_facet(trace_id))

    # OpenLineage asks every run for one START and one ending. A one-shot run — a promotion, a
    # label move, a retrain request, a cost recording — has nothing to report until it is over,
    # so its ending is its first event; the receiver is then sent a START ahead of it.
    url = os.getenv("EXAMLOPS_OPENLINEAGE_URL")
    opens_run = bool(url) and event_type in _ENDINGS and not _run_seen(run_id)

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
    if not url:
        return
    try:
        if opens_run:
            _post(url, _openlineage_event("START", job, run_id, inputs, outputs, facets))
        _post(url, _openlineage_event(event_type, job, run_id, inputs, outputs, facets))
    except Exception as exc:
        logger.warning("OpenLineage emit to %s failed (ignored): %s", url, exc)


_ENDINGS = frozenset({"COMPLETE", "FAIL", "ABORT"})


def _run_seen(run_id: str) -> bool:
    """Whether this run already has an event. Unknown (datastore down) counts as not seen: a
    duplicate START is harmless to a receiver, and a run with no START breaks the protocol."""
    try:
        from examlops.data.events import lineage_run_seen

        return lineage_run_seen(run_id)
    except Exception:  # noqa: BLE001
        return False


def attach_run_cost(
    mlflow_run_id: str,
    *,
    gpu_hours: float,
    cost_usd: float | None = None,
    kwh: float = 0.0,
    co2e_kg: float = 0.0,
) -> bool:
    """Record what a training run cost as a child run of it (clause 2).

    Cost is known only after the scheduler has accounted the job (``exa models cost --record``),
    long after the run ended. Each recording is therefore its own run — job ``cost:<model>``,
    ``COMPLETE``, the ``examlops.cost`` facet — whose standard ``parent`` facet names the training
    run. The spec's other option, an ``OTHER`` event on the finished run, is what it suggests for
    late metadata, and Marquez 0.51.1 answers it by showing the run as RUNNING again: the finished
    run must stay finished. The run is found through the MLflow run it registered; ``False`` when
    no training lineage names that MLflow run. Fail-open like every emit.
    """
    try:
        from examlops.data.events import lineage_run_for_mlflow_run

        parent = lineage_run_for_mlflow_run(mlflow_run_id)
        if parent is None:
            return False
        model = parent["job"].split(":", 1)[-1]
        emit_lineage(
            "COMPLETE",
            f"cost:{model}",
            f"cost:{mlflow_run_id}:{uuid.uuid4().hex}",  # every recording is its own run
            facets={
                **parent_facet(parent["run_id"], parent["job"]),
                **cost_facet(gpu_hours, kwh, co2e_kg, cost_usd=cost_usd),
            },
            mlflow_run_id=mlflow_run_id,
            model=parent["model"],
        )
        return True
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the command that asked
        logger.warning("lineage cost record failed: %s", exc)
        return False


def current_trace_id() -> str | None:
    """The active OpenTelemetry trace id (32 hex digits, as Tempo shows it), or ``None``.

    ``None`` when no span is recording — the default, with ``OTEL_SDK_DISABLED=true`` — and when
    the OpenTelemetry API is not installed.
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415

        context = trace.get_current_span().get_span_context()
    except Exception:  # noqa: BLE001 - tracing is optional
        return None
    return format(context.trace_id, "032x") if context.is_valid else None


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
