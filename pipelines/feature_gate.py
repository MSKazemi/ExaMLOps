"""Training-side feature gate: one train/serve definition, enforced (ADR 0017 clauses 2 + 3).

A model YAML dataset entry binds a pack feature view (``feature_view: <name>``). Before training,
:func:`feature_view_gate` runs the pinned training data through the **same** ``transform_row``
serving applies to every inference request, under the **same** definition file:

1. every required feature must exist as a column and every sampled row must satisfy its declared
   type and dimension — a training set serving would reject is refused here (fails closed);
2. when the view names its entity column (``entity_column``, else ``entity_key``) and its
   ``timestamp_field`` and both are columns, the validated rows are appended to the offline store
   (idempotent on entity + UTC event time) and read back with point-in-time retrieval
   (``get_training_features``): any row whose as-of value differs from what the model trains on
   is a mismatch — clause 3 on the real training path. An entity + event time that carries two
   different values *within* the training data is ambiguous: it is kept out of the offline store
   and counted (``pit_ambiguous``), not guessed. Whether the check ran is always in ``pit``;
3. the view's fingerprint is returned so the run can be tagged with it
   (``feature_view`` / ``feature_view_fingerprint``) — the record of which definition trained it.

Mode: ``EXAMLOPS_FEATURE_GATE`` = ``enforce`` (default) | ``warn`` | ``off``. Like the data-contract
gate, things it cannot check are reported as skips with a reason, never as a pass: no binding,
a ``--dummy`` run, or data whose location is not readable here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

#: Rows validated per table by default — bounds memory and offline-store writes per run.
DEFAULT_MAX_ROWS = 1000


class FeatureViewViolation(RuntimeError):
    """The training data does not satisfy its bound feature view (ADR 0017 clause 2)."""


def gate_mode() -> str:
    mode = os.environ.get("EXAMLOPS_FEATURE_GATE", "enforce").strip().lower()
    return mode if mode in ("enforce", "warn", "off") else "enforce"


def _max_rows() -> int:
    try:
        return max(1, int(os.environ.get("EXAMLOPS_FEATURE_GATE_MAX_ROWS", str(DEFAULT_MAX_ROWS))))
    except ValueError:
        return DEFAULT_MAX_ROWS


def _ingest_enabled() -> bool:
    return os.environ.get("EXAMLOPS_FEATURE_GATE_INGEST", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _event_ts(value: Any) -> str | None:
    """A dataset timestamp as the offline store's UTC ``YYYY-MM-DD HH:MM:SS`` string.

    A zone-aware value is converted to UTC: FData's ``adt`` reads ``2021-04-01 10:28:01+09``, and
    cutting the ``+09`` off would file every event nine hours late next to UTC-stamped sources. A
    naive value is taken as already UTC, the store's own convention.
    """
    if value is None or (isinstance(value, float) and value != value):
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        text = str(value).strip()
        if not text or text.lower() in ("nat", "nan", "none"):
            return None
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    try:
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):  # NaT and out-of-range values carry no event time
        return None


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and value != value


def _violation(report: dict[str, Any], message: str) -> dict[str, Any]:
    report.update({"validated": True, "passed": False, "failure": message})
    if report["gate"] == "enforce":
        raise FeatureViewViolation(message)
    print(f"[feature-gate] FAILED but gate=warn — continuing: {message}")
    return report


def feature_view_gate(
    dataset_name: str,
    *,
    view_name: str | None,
    is_dummy: bool = False,
    load_inputs: Callable[[], tuple[list[tuple[str, Any]] | None, str]] | None = None,
    definitions_dir: str | None = None,
) -> dict[str, Any]:
    """Validate the pinned training data against its bound feature view. See module docstring.

    ``load_inputs`` returns ``([(table, load), …], source)`` or ``(None, reason)`` — the same
    bounded loader the data-contract gate uses, so both gates read the data this run trains on.
    """
    report: dict[str, Any] = {"dataset": dataset_name, "gate": gate_mode(), "view": view_name}
    if report["gate"] == "off":
        return {**report, "validated": False, "reason": "gate disabled"}
    if not view_name:
        return {**report, "validated": False, "reason": "no feature view bound"}

    from examlops.feature_store.definitions import load_definitions

    defs = load_definitions(definitions_dir)
    if defs.errors:
        return _violation(report, "feature definitions invalid: " + "; ".join(defs.errors))
    view = defs.views.get(view_name)
    if view is None:
        return _violation(
            report, f"feature view '{view_name}' is not declared under {defs.directory}"
        )
    report["fingerprint"] = view.fingerprint()
    try:
        from examlops.feature_store.definitions import sync_definitions

        sync_definitions(definitions_dir, actor="pipeline")
        report["registry"] = "synced"
    except Exception as exc:  # noqa: BLE001 - the file is the definition; the registry mirrors it
        report["registry"] = f"sync skipped: {type(exc).__name__}: {exc}"

    if is_dummy:
        return {**report, "validated": False, "reason": "dummy run — synthetic rows"}
    if load_inputs is None:
        return {**report, "validated": False, "reason": "no data loader"}
    try:
        inputs, source = load_inputs()
    except Exception as exc:  # noqa: BLE001 - an unreadable location is a skip, said as one
        return {**report, "validated": False, "reason": f"data unreadable: {exc}"}
    if inputs is None:
        return {**report, "validated": False, "reason": source}

    from examlops.feature_store.spec import FeatureValidationError, transform_row

    cap = _max_rows()
    checked = failures = 0
    messages: list[str] = []
    keyed_rows: dict[tuple[str, str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str]] = set()
    unkeyed = 0
    pit_skips: list[str] = []
    for table, load in inputs:
        frame = load().frame
        columns = set(getattr(frame, "columns", []))
        missing = [s.name for s in view.features if s.required and s.name not in columns]
        if missing:
            where = f" table '{table}'" if table else ""
            return _violation(
                report,
                f"training data{where} lacks feature column(s) {missing} required by "
                f"view '{view.name}'",
            )
        entity_col = view.dataset_entity_column
        keyed = bool(
            entity_col
            and view.timestamp_field
            and entity_col in columns
            and view.timestamp_field in columns
        )
        if entity_col and view.timestamp_field and not keyed:
            # Said, never silent: a view promising point-in-time keys the data lacks is a skip.
            where = f" in table '{table}'" if table else ""
            pit_skips.append(
                f"columns {entity_col!r}/{view.timestamp_field!r} not both present{where}"
            )
        for record in frame.head(cap).to_dict(orient="records"):
            checked += 1
            try:
                values = transform_row(view, record)
            except FeatureValidationError as exc:
                failures += 1
                if len(messages) < 5:
                    messages.append(str(exc))
                continue
            if not keyed:
                continue
            ts = _event_ts(record.get(view.timestamp_field))
            entity = record.get(entity_col)
            if ts is None or entity is None or _is_nan(entity):
                unkeyed += 1
                continue
            key = (str(entity), ts)
            if key in ambiguous:
                continue
            prior = keyed_rows.get(key)
            if prior is None:
                keyed_rows[key] = values
            elif prior != values:
                # One entity, one event time, two different values in the training data itself:
                # no retrieval can be point-in-time correct for it. Keep it out of the offline
                # log (storing an arbitrary one would later be served as the truth); report it.
                del keyed_rows[key]
                ambiguous.add(key)
    report.update({"source": source, "rows_checked": checked, "row_failures": failures})
    if failures:
        return _violation(
            report,
            f"{failures}/{checked} training rows violate feature view '{view.name}': "
            + "; ".join(messages),
        )
    offline_rows = [
        {"entity_id": e, "event_ts": t, "values": v} for (e, t), v in keyed_rows.items()
    ]
    if ambiguous:
        report["pit_ambiguous"] = len(ambiguous)
    if unkeyed:
        report["pit_unkeyed_rows"] = unkeyed
    if pit_skips:
        report["pit"] = "skipped: " + "; ".join(pit_skips)
    elif not (view.dataset_entity_column and view.timestamp_field):
        report["pit"] = "skipped: the view declares no entity + timestamp key"
    elif not _ingest_enabled():
        report["pit"] = "skipped: EXAMLOPS_FEATURE_GATE_INGEST is off"
    elif not offline_rows:
        report["pit"] = "skipped: no row carried an unambiguous entity + event time"
    else:
        report["pit"] = "checked"
    if offline_rows and _ingest_enabled():
        _point_in_time(view.name, offline_rows, report)
        _audit(view.name, dataset_name, report)
        if report.get("pit_mismatches"):
            return _violation(
                report,
                f"{report['pit_mismatches']} row(s) of '{view.name}' read back differently "
                "through point-in-time retrieval than the model trains on (the offline store "
                "already holds a different value for the same entity + event time)",
            )
    report.update({"validated": True, "passed": True})
    return report


def _point_in_time(view: str, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    """Ingest the validated rows, then read each back as of its event time and compare."""
    from examlops.feature_store import get_training_features, ingest_rows

    written = ingest_rows(view, rows)
    asof = get_training_features(
        view, [{"entity_id": r["entity_id"], "event_ts": r["event_ts"]} for r in rows]
    )
    mismatches = sum(1 for r, got in zip(rows, asof, strict=True) if got != r["values"])
    report.update(
        {
            "offline_written": written["written"],
            "offline_skipped": written["skipped"],
            "pit_checked": len(rows),
            "pit_mismatches": mismatches,
        }
    )


def _audit(view: str, dataset: str, report: dict[str, Any]) -> None:
    try:
        from examlops.data.audit import audit_best_effort

        audit_best_effort(
            "pipeline",
            os.environ.get("EXAMLOPS_ACTOR") or "pipeline",
            "feature_gate_ingested",
            view,
            {
                "dataset": dataset,
                "fingerprint": report.get("fingerprint"),
                "offline_written": report.get("offline_written"),
                "offline_skipped": report.get("offline_skipped"),
                "pit_mismatches": report.get("pit_mismatches"),
            },
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never decides the gate
        print(f"[feature-gate] audit skipped: {exc}")


def validation_tag(report: dict[str, Any]) -> str:
    """``passed`` | ``failed`` (warn mode) | ``skipped: <reason>`` — the run's gate outcome."""
    if report.get("validated"):
        return "passed" if report.get("passed") else "failed"
    return f"skipped: {report.get('reason') or 'not validated'}"[:250]


def tag_run(run_id: str | None, report: dict[str, Any]) -> bool:
    """Tag the MLflow run with the feature view that trained it. Best-effort; returns success."""
    if not run_id or not report.get("fingerprint"):
        return False
    try:
        from mlflow.tracking import MlflowClient

        client = MlflowClient()
        client.set_tag(run_id, "feature_view", str(report["view"]))
        client.set_tag(run_id, "feature_view_fingerprint", str(report["fingerprint"]))
        # The fingerprint names the definition this run was bound to, not proof that the data
        # was checked against it: a skipped gate (unreadable data, a dummy run) says so here.
        client.set_tag(run_id, "feature_view_validated", validation_tag(report))
        return True
    except Exception as exc:  # noqa: BLE001 - a tag must never fail a finished training run
        print(f"[feature-gate] MLflow tag skipped: {exc}")
        return False
