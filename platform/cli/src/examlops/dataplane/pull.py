"""Source definitions and the pull orchestrator (ADR 0130 §6-7)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops.coordination import get_coordinator
from examlops.data import dataplane as catalog
from examlops.dataplane import store as st
from examlops.dataplane.connectors import registry
from examlops.dataplane.metrics import observe_pull
from examlops.dataplane.safety import redact, validate_name
from examlops.dataplane.types import (
    ConnectorUnavailable,
    DataplaneError,
    Limits,
    Probe,
    PullInProgress,
    SnapshotNotFound,
    SpecError,
    global_limits,
)
from examlops.resilience.retry import is_transient_network, retry_call

_SECRETISH = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
)


@dataclass(frozen=True)
class SourceDef:
    project: str
    name: str
    connector: str
    connection: str | None
    spec: dict[str, Any] = field(default_factory=dict)
    schedule: str | None = None
    limits: Limits = field(default_factory=Limits)
    contract: str | None = None
    enabled: bool = True

    @property
    def key(self) -> str:
        return st.source_key(self.project, self.name)

    @property
    def spec_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PullResult:
    pull_id: str
    status: str
    revision: str | None
    row_count: int
    byte_count: int
    manifest_uri: str | None
    error: str | None = None


def _actor(actor: str | None) -> str:
    return actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "dataplane"


def _audit(action: str, target: str, details: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("dataplane", _actor(actor), action, target, details)
    except Exception:
        pass  # audit is best-effort here, as elsewhere in the platform


def _reject_secret_keys(obj: Any, path: str = "spec") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SECRETISH):
                raise SpecError(
                    f"{path}.{k} looks like a credential — put it in a Named Connection "
                    "(exa connection create … --secret) and reference the connection"
                )
            _reject_secret_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_secret_keys(v, f"{path}[{i}]")


def _row_to_def(row: dict[str, Any]) -> SourceDef:
    return SourceDef(
        project=row["project"],
        name=row["name"],
        connector=row["connector"],
        connection=row.get("connection"),
        spec=row.get("spec") or {},
        schedule=row.get("schedule"),
        limits=Limits.from_dict(row.get("limits")),
        contract=row.get("contract"),
        enabled=bool(row.get("enabled", 1)),
    )


def define_source(
    name: str,
    connector: str,
    *,
    project: str = "",
    connection: str | None = None,
    spec: dict[str, Any] | None = None,
    schedule: str | None = None,
    limits: Limits | None = None,
    contract: str | None = None,
    enabled: bool = True,
    actor: str | None = None,
) -> SourceDef:
    validate_name(name, "source name")
    spec = dict(spec or {})
    _reject_secret_keys(spec)
    c = registry.get(connector)
    errors = c.validate_spec(spec)
    if spec.get("incremental") and not c.supports_incremental:
        errors.append(f"connector {connector!r} does not support incremental pulls")
    if errors:
        raise SpecError("; ".join(errors))
    if connection:
        from examlops.connections import get_connection

        row = get_connection(connection, project=project or None)
        if row is None:
            raise SpecError(f"connection {connection!r} not found (project={project or '-'})")
        if c.connection_kinds and row["kind"] not in c.connection_kinds:
            raise SpecError(
                f"connector {connector!r} accepts connection kinds "
                f"{list(c.connection_kinds)}, not {row['kind']!r}"
            )
    elif c.connection_required:
        raise SpecError(f"connector {connector!r} needs --connection")
    if schedule:
        from examlops.dataplane.service.scheduler import parse_interval

        parse_interval(schedule)  # raises SpecError on a bad value
    existed = catalog.get_source(name, project) is not None
    catalog.upsert_source(
        project,
        name,
        connector=connector,
        connection=connection,
        spec=spec,
        schedule=schedule,
        limits=(limits or Limits()).to_dict(),
        contract=contract,
        enabled=enabled,
        actor=_actor(actor),
    )
    _audit(
        "dataplane_source_updated" if existed else "dataplane_source_created",
        st.source_key(project, name),
        {"connector": connector, "connection": connection},
        actor,
    )
    return get_source_def(name, project)


def get_source_def(name: str, project: str = "") -> SourceDef:
    row = catalog.get_source(name, project)
    if row is None:
        raise SpecError(f"source {name!r} not found (project={project or '-'})")
    return _row_to_def(row)


def list_source_defs(project: str | None = None) -> list[SourceDef]:
    return [_row_to_def(r) for r in catalog.list_sources(project)]


def remove_source(name: str, project: str = "", actor: str | None = None) -> bool:
    removed = catalog.delete_source(name, project)
    if removed:
        _audit("dataplane_source_deleted", st.source_key(project, name), {}, actor)
    return removed


def _resolved_connection(src: SourceDef, actor: str | None) -> dict[str, Any] | None:
    if not src.connection:
        return None
    from examlops.connections import resolve_connection

    return resolve_connection(src.connection, project=src.project or None, actor=_actor(actor))


def _load_contract(name: str) -> Any:
    try:
        from pipelines.contracts import load_contract  # the dataplane image installs pipelines
    except Exception as exc:
        raise SpecError(f"ingestion contract {name!r} needs the pipelines package: {exc}") from None
    contract = load_contract(name)
    if contract is None:
        raise SpecError(f"no data contract named {name!r}")
    return contract


def _check_contract(name: str, staged: list[tuple[str, Path]]) -> None:
    import pandas as pd

    contract = _load_contract(name)
    df = (
        pd.concat([pd.read_parquet(p) for _, p in staged], ignore_index=True)
        if staged
        else pd.DataFrame()
    )
    result = contract.validate(df)
    if not result.passed:
        failures = "; ".join(f"{c.get('name')} ({c.get('observed')})" for c in result.errors)
        raise DataplaneError(
            f"ingestion contract {name!r} failed (score {result.score}): {failures}"
        )


def _record_revision(
    src: SourceDef, manifest: st.SnapshotManifest, store: st.DatasetStore, actor: str | None
) -> None:
    from types import SimpleNamespace

    from examlops.data.data_assets import record_dataset_revision

    rev = SimpleNamespace(
        backend="dataplane",
        dataset=src.key,
        revision_id=manifest.revision,
        kind="dataplane",
        uri=store.uri(f"{src.key}/{manifest.pull_id}/_manifest.json"),
        schema_hash=manifest.schema_hash,
    )
    record_dataset_revision(
        rev, row_count=manifest.row_count, byte_count=manifest.byte_count, actor=_actor(actor)
    )


def _announce(src: SourceDef, manifest: st.SnapshotManifest, pull_id: str) -> None:
    try:
        from examlops.events import publish

        publish(
            "dataplane.snapshot.committed",
            {"source": src.key, "revision": manifest.revision, "rows": manifest.row_count},
        )
    except Exception:
        pass
    try:
        from examlops.lineage import Node, dataset_node, emit_lineage

        emit_lineage(
            "COMPLETE",
            job=f"dataplane.pull.{src.key}",
            run_id=pull_id,
            inputs=[Node(f"{src.connector}:{src.connection or '-'}:{src.name}")],
            outputs=[dataset_node(src.key, manifest.revision)],
            dataset_revision=manifest.revision,
        )
    except Exception:
        pass


def run_pull(
    name: str,
    *,
    project: str = "",
    trigger_kind: str = "manual",
    actor: str | None = None,
    full: bool = False,
    store: st.DatasetStore | None = None,
    stage_root: Path | None = None,
    pull_id: str | None = None,
) -> PullResult:
    src = get_source_def(name, project)
    if not src.enabled:
        raise SpecError(f"source {src.key} is disabled")
    connector = registry.get(src.connector)
    ok, why = connector.available()
    if not ok:
        raise ConnectorUnavailable(f"connector {src.connector!r} unavailable: {why}")
    limits = src.limits.capped(global_limits())
    pull_id = pull_id or catalog.new_pull_id()
    coord = get_coordinator()
    lock_key = f"dataplane:pull:{src.key}"
    if not coord.try_lock(lock_key, pull_id, ttl_s=limits.max_seconds or 3600.0):
        raise PullInProgress(f"a pull of {src.key} is already running")
    started = time.monotonic()
    secret: str | None = None
    try:
        store = store or st.store_from_env()
        try:
            parent: st.SnapshotManifest | None = st.read_manifest(store, st.resolve(store, src.key))
        except SnapshotNotFound:
            parent = None
        incremental = bool(src.spec.get("incremental")) and not full and parent is not None
        since = parent.watermark if incremental and parent else None
        catalog.insert_pull(
            pull_id,
            src.project,
            src.name,
            trigger_kind=trigger_kind,
            actor=_actor(actor),
            parent_revision=parent.revision if parent else None,
        )
        conn = _resolved_connection(src, actor)
        secret = (conn or {}).get("secret")

        def _read_attempt():
            """One complete read into a fresh stage dir.

            A transient failure mid-read discards this attempt's stage dir entirely (never
            merges partial parts from a failed attempt with a retried one) and lets
            ``retry_call`` start over from scratch.
            """
            tmpdir = tempfile.TemporaryDirectory(prefix=f"dp-{pull_id}-", dir=stage_root)
            try:
                writer = st.SnapshotWriter(Path(tmpdir.name), limits=limits)
                for tb in connector.read(conn, src.spec, since, limits):
                    writer.write(tb)
                staged = writer.close()
                return staged, writer.watermark, tmpdir
            except Exception:
                tmpdir.cleanup()
                raise

        staged, watermark, tmpdir = retry_call(
            _read_attempt,
            retries=2,
            retry_on=is_transient_network,
            label=f"dataplane pull {src.key}",
        )
        try:
            if src.contract:
                _check_contract(src.contract, staged)
            catalog.update_pull(pull_id, status="committing")
            manifest, changed = st.publish(
                store,
                src.key,
                staged=staged,
                parent=parent,
                connector=src.connector,
                connection=src.connection,
                spec_hash=src.spec_hash,
                watermark=watermark or (parent.watermark if parent else {}),
                pull_id=pull_id,
                incremental=incremental,
            )
        finally:
            tmpdir.cleanup()
        status = "succeeded" if changed else "unchanged"
        catalog.update_pull(
            pull_id,
            status=status,
            finished=True,
            revision=manifest.revision,
            row_count=manifest.row_count,
            byte_count=manifest.byte_count,
            watermark=manifest.watermark,
        )
        if changed:
            _record_revision(src, manifest, store, actor)
            _announce(src, manifest, pull_id)
            _audit(
                "dataplane_pull_succeeded",
                src.key,
                {"pull_id": pull_id, "revision": manifest.revision, "rows": manifest.row_count},
                actor,
            )
        observe_pull(
            src.key, status, time.monotonic() - started, manifest.row_count, manifest.byte_count
        )
        return PullResult(
            pull_id,
            status,
            manifest.revision,
            manifest.row_count,
            manifest.byte_count,
            store.uri(f"{src.key}/{manifest.pull_id}/_manifest.json"),
        )
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}", secrets=[secret] if secret else [])
        try:
            catalog.update_pull(pull_id, status="failed", finished=True, error=message)
        except Exception:
            pass
        _audit("dataplane_pull_failed", src.key, {"pull_id": pull_id, "error": message}, actor)
        observe_pull(src.key, "failed", time.monotonic() - started, 0, 0)
        if isinstance(exc, DataplaneError):
            raise type(exc)(message) from None
        raise DataplaneError(message) from None
    finally:
        coord.unlock(lock_key, pull_id)


def preview(name: str, *, project: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Read at most ``limit`` rows; nothing is written anywhere."""
    src = get_source_def(name, project)
    connector = registry.get(src.connector)
    conn = _resolved_connection(src, None)
    secret = (conn or {}).get("secret")
    rows: list[dict[str, Any]] = []
    try:
        for tb in connector.read(conn, src.spec, None, Limits(max_rows=limit)):
            rows.extend(tb.batch.to_pylist())
            if len(rows) >= limit:
                break
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}", secrets=[secret] if secret else [])
        if isinstance(exc, DataplaneError):
            raise type(exc)(message) from None
        raise DataplaneError(message) from None
    return rows[:limit]


def test_source(name: str, *, project: str = "") -> Probe:
    src = get_source_def(name, project)
    connector = registry.get(src.connector)
    ok, why = connector.available()
    if not ok:
        return Probe(False, why)
    conn = _resolved_connection(src, None)
    secret = (conn or {}).get("secret")
    try:
        return connector.probe(conn, src.spec)
    except Exception as exc:
        return Probe(
            False, redact(f"{type(exc).__name__}: {exc}", secrets=[secret] if secret else [])
        )


__all__ = [
    "PullResult",
    "SourceDef",
    "define_source",
    "get_source_def",
    "list_source_defs",
    "preview",
    "remove_source",
    "run_pull",
    "test_source",
]
