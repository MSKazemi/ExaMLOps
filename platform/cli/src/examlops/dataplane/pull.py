"""Source definitions and the pull orchestrator (ADR 0130 §6-7)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from examlops.coordination import get_coordinator
from examlops.data import dataplane as catalog
from examlops.dataplane import store as st
from examlops.dataplane.connectors import registry
from examlops.dataplane.lease import LeaseHeartbeat
from examlops.dataplane.metrics import observe_pull
from examlops.dataplane.safety import redact, validate_name
from examlops.dataplane.types import (
    ConnectorUnavailable,
    DataplaneError,
    IncrementalInvalidated,
    Limits,
    Probe,
    PullInProgress,
    SnapshotNotFound,
    SpecError,
    global_limits,
)
from examlops.resilience.retry import is_transient_network, retry_call

logger = logging.getLogger(__name__)

# Task 22a: the exact prefix `run_pull` passes to `tempfile.TemporaryDirectory` for a pull's stage
# dir (`dp-<pull_id>-<random>`). Shared with `cleanup_stale_stage_dirs` so the two can never drift.
_STAGE_DIR_PREFIX = "dp-"
# How long a fresh reaper holder holds a source's coordinator lock before releasing it again —
# only long enough to make the failed-status update and release, never held idle.
_REAP_LOCK_TTL_S = 30.0
# The lease `prune_source` holds on a source's pull lock; renewed by `_LeaseHeartbeat` like a pull's.
_PRUNE_LOCK_TTL_S = 300.0
# How long `_LeaseHeartbeat.stop()` waits for an in-flight renewal before releasing anyway (a late
# renewal then gives the lock straight back itself).
_HEARTBEAT_JOIN_S = 10.0
# This process's start time, captured once at import — the fixed reference `cleanup_stale_stage_
# dirs` compares stage-dir mtimes against, so it never drifts forward on repeated calls the way
# re-reading `time.time()` on each call would.
_PROCESS_START = time.time()

# Substrings that mark a spec key as a credential carrier, matched against the key lower-cased with
# `-`, `_`, `.` and whitespace removed (`_normalized_key`) — so `X-API-Key`, `x_api_key` and
# `XApiKey` are one key, and `Authorization`/`Cookie`/`credentials` in `spec.headers` are caught.
# The pre-normalization spellings (`api_key`, `access_key`, `private_key`) are covered by their
# normalized forms. Bare `auth` is handled separately in `_is_secretish` (`author` is not one).
_SECRETISH = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
)


# Inside `spec.headers` the rule is stricter. A header is exactly how an API credential travels,
# and real ones escape the list above: `X-Auth-Key`, `Ocp-Apim-Subscription-Key`,
# `X-Functions-Key`, `X-Amz-Signature`, `oauth2`, `jwt`, `session_id`. Any header whose normalised
# name contains one of these is refused. Only headers: as a general key, `key`/`sig`/`session`
# would refuse ordinary spec fields (`key_column`, `signature_col`, `session_window`).
_HEADER_SECRETISH = (
    "key",
    "token",
    "auth",
    "secret",
    "sig",
    "session",
    "cookie",
    "jwt",
    "password",
    "credential",
)
# Query parameters that carry a credential, normalised (lowercase, no `-`/`_`/`.`). Exact names,
# not substrings: `sort_key`, `country_code` and `page_token` are ordinary parameters.
_QUERY_CREDENTIALS = frozenset({
    "sig", "signature", "xamzsignature", "xamzcredential", "xamzsecuritytoken",
    "xgoogsignature", "xgoogcredential", "token", "accesstoken", "idtoken", "refreshtoken",
    "authtoken", "apitoken", "apikey", "key", "code", "password", "passwd", "pwd", "secret",
    "clientsecret", "auth", "authorization", "session", "sessionid", "sid", "jwt",
    "credential", "credentials", "accesskey", "accesskeyid", "awsaccesskeyid", "secretkey",
    "privatetoken", "subscriptionkey", "oauthtoken", "xapikey", "authkey", "passphrase",
    "bearer",
})  # fmt: skip
# A string that starts `scheme://` is a URL, whatever key it sits under.
_URL_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _normalized_key(key: Any) -> str:
    return "".join(ch for ch in str(key).lower() if ch not in "-_." and not ch.isspace())


def _is_secretish(key: Any) -> bool:
    k = _normalized_key(key)
    if any(s in k for s in _SECRETISH):
        return True
    # `auth`, `x-auth`, `basic_auth`, `oauth`, `auth_header` — but not `author`/`authority`.
    return k.endswith("auth") or (k.startswith("auth") and not k.startswith("author"))


def _is_secretish_header(name: Any) -> bool:
    """The stricter ``spec.headers`` rule (``_HEADER_SECRETISH``), on top of ``_is_secretish``.

    ``author``/``authority`` stay allowed here as everywhere else, and so does ``keyword`` (a
    search parameter, not a key); ``Authorization`` is caught by the general list first."""
    if _is_secretish(name):
        return True
    k = _normalized_key(name)
    for benign in ("authority", "author", "keyword"):
        k = k.replace(benign, "")
    return any(s in k for s in _HEADER_SECRETISH)


def _url_has_password(value: str) -> bool:
    """True for a URL whose userinfo carries a password (``https://user:pw@host``,
    ``sftp://u:p@h``). A bare ``user@`` carries no secret and is fine."""
    if not _URL_PREFIX.match(value):
        return False
    try:
        return bool(urlsplit(value).password)
    except ValueError:  # a malformed netloc: the connector's own validation reports it
        return False


def _url_query_credential(value: str) -> str | None:
    """The name of a credential-carrying query parameter in a URL, else ``None``.

    Matched EXACTLY against ``_QUERY_CREDENTIALS`` (after normalising case, ``-``/``_``/``.``):
    query parameters are ordinary API vocabulary, and the substring rule used for headers would
    refuse ``sort_key``, ``country_code`` or ``page_token``. A presigned URL (``X-Amz-Signature``,
    ``sig``) or a ``?token=``/``?api_key=`` pasted into a spec is a stored secret."""
    if not _URL_PREFIX.match(value):
        return None
    try:
        query = urlsplit(value).query
    except ValueError:
        return None
    for name, _value in parse_qsl(query, keep_blank_values=True):
        if _normalized_key(name) in _QUERY_CREDENTIALS:
            return name
    return None


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
    # What the ingestion contract checked (``_check_contract``'s report), ``None`` without one:
    # ``{"name", "sampled", "tables": {table: {"rows_checked", "rows", "sampled"}}}``.
    contract: dict[str, Any] | None = None


def _actor(actor: str | None) -> str:
    return actor or os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "dataplane"


def _audit(action: str, target: str, details: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("dataplane", _actor(actor), action, target, details)
    except Exception:
        pass  # audit is best-effort here, as elsewhere in the platform


def _reject_secret_keys(obj: Any, path: str = "spec", *, headers: bool = False) -> None:
    """Refuse (never strip) a credential anywhere in a spec, nested dicts and lists included
    (``spec.headers``, ``spec.params``, …): a credential-shaped key (a stricter rule inside
    ``headers``), or a URL-valued string with a password in its userinfo. The error names the
    key's path, never its value: a spec is stored in plaintext and served back by
    ``GET /sources``/``sources show``.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_secretish(k) or (headers and _is_secretish_header(k)):
                raise SpecError(
                    f"{path}.{k} looks like a credential — put it in a Named Connection "
                    "(exa connection create … --secret) and reference the connection"
                )
            _reject_secret_keys(v, f"{path}.{k}", headers=_normalized_key(k) == "headers")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_secret_keys(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        if _url_has_password(obj):
            raise SpecError(
                f"{path} is a URL with a password in it (user:password@host) — put the credential "
                "in a Named Connection (exa connection create … --secret) and reference the "
                "connection; a bare user@host is fine"
            )
        param = _url_query_credential(obj)
        if param is not None:
            raise SpecError(
                f"{path} is a URL whose query carries a credential ({param!r} parameter) — a "
                "presigned or tokened URL is a stored secret; put the credential in a Named "
                "Connection and reference the connection"
            )


def pull_lock_key(source_key: str) -> str:
    """The coordinator lock one pull of a source holds (``dataplane:pull:<source key>``).

    Shared with the service's scheduler, which reserves it before queueing a pull.
    """
    return f"dataplane:pull:{source_key}"


def _lease_ttl_s(limits: Limits) -> float:
    """The lease on a source's pull lock: the same TTL the scheduler reserves a queued pull with.

    While the pull runs, ``_LeaseHeartbeat`` renews it every third of the TTL, so it bounds how
    long a *crashed* pull keeps the lock — not how long a live one may take."""
    return limits.max_seconds or 3600.0


class _LeaseHeartbeat(LeaseHeartbeat):
    """Backward-compatible name for :class:`examlops.dataplane.lease.LeaseHeartbeat` (moved there
    in task A4 so stream leader election can reuse it too).

    A thin subclass rather than a plain ``_LeaseHeartbeat = LeaseHeartbeat`` re-export: this
    module's own ``_HEARTBEAT_JOIN_S`` is bound here, at construction time, as the generic class's
    ``join_s`` — reading it as a global inside code that (unlike the base class) lives in this
    module, so ``pull.py``'s own tuning (and the existing test that monkeypatches
    ``_HEARTBEAT_JOIN_S``) keeps working unchanged. Behaviour is otherwise identical to the base
    class: same renew-every-``ttl_s/3`` loop, same ``lost`` flag, same ``stop()`` semantics.
    """

    def __init__(self, coord: Any, key: str, holder: str, ttl_s: float) -> None:
        super().__init__(coord, key, holder, ttl_s, join_s=_HEARTBEAT_JOIN_S)


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


CONTRACT_MAX_ROWS_ENV = "EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS"
_CONTRACT_MAX_ROWS_DEFAULT = 200_000
# Rows per Arrow batch while streaming a part — bounds the read-ahead past the cap.
_CONTRACT_BATCH_ROWS = 65_536


def contract_max_rows() -> int:
    """How many rows of one table a data-contract check reads (``EXAMLOPS_DATAPLANE_CONTRACT_MAX_
    ROWS``, default 200000). An unreadable or non-positive value falls back to the default with a
    warning: a typo must neither disable the check nor remove the memory bound."""
    raw = os.getenv(CONTRACT_MAX_ROWS_ENV, "").strip()
    if not raw:
        return _CONTRACT_MAX_ROWS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "dataplane: %s=%r is not a positive integer; using %d",
            CONTRACT_MAX_ROWS_ENV,
            raw,
            _CONTRACT_MAX_ROWS_DEFAULT,
        )
        return _CONTRACT_MAX_ROWS_DEFAULT
    return value


@dataclass(frozen=True)
class ContractSample:
    """At most ``contract_max_rows()`` rows of ONE table, as the DataFrame a contract validates.

    ``rows`` were read; ``total_rows`` is the table's real size (Parquet footers, no data read);
    ``sampled`` is true when rows were left unread. ``frame.attrs["total_rows"]`` carries the real
    size to checks that judge the table's size rather than its values (``min_rows``)."""

    frame: Any
    rows: int
    total_rows: int
    sampled: bool

    def summary(self) -> dict[str, Any]:
        return {"rows_checked": self.rows, "rows": self.total_rows, "sampled": self.sampled}


def read_contract_sample(parts: Iterable[Path], max_rows: int | None = None) -> ContractSample:
    """Stream one table's Parquet ``parts`` in order and stop after ``max_rows`` rows.

    Only the rows kept are ever materialized (plus at most one Arrow batch of read-ahead); a part
    past the cap is opened for its footer only. A table with no rows keeps its columns, so a
    column check still sees them."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    cap = max_rows if max_rows is not None else contract_max_rows()
    frames: list[Any] = []
    schema: Any = None
    rows = total = 0
    for path in parts:
        with pq.ParquetFile(path) as pf:
            n = pf.metadata.num_rows
            total += n
            if schema is None:
                schema = pf.schema_arrow
            remaining = cap - rows
            if n == 0 or remaining <= 0:
                continue  # past the cap: the footer's row count is all that is read
            batches = []
            for batch in pf.iter_batches(batch_size=min(_CONTRACT_BATCH_ROWS, remaining)):
                take = min(batch.num_rows, remaining)
                batches.append(batch if take == batch.num_rows else batch.slice(0, take))
                remaining -= take
                if remaining <= 0:
                    break
            if batches:
                table = pa.Table.from_batches(batches)
                rows += table.num_rows
                frames.append(table.to_pandas())
    if frames:
        frame = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    elif schema is not None:
        frame = schema.empty_table().to_pandas()
    else:
        frame = pd.DataFrame()
    frame.attrs["total_rows"] = total
    return ContractSample(frame, rows, total, rows < total)


def _table_of(rel: str) -> str:
    """The table a staged part belongs to: ``jobs/part-00000.parquet`` → ``jobs``."""
    return PurePosixPath(rel).parts[0]


def _check_contract(name: str, staged: list[tuple[str, Path]]) -> dict[str, Any]:
    """Run the source's ingestion contract over the staged parts, ONE table at a time.

    Tables are never concatenated: the contract's own ``table`` is the one checked; a contract
    that names none is checked against every table separately. Each table is read through
    ``read_contract_sample`` (at most ``contract_max_rows()`` rows). A table the pull did not
    produce is checked as empty, as an empty pull always was. Returns the report ``PullResult``
    and the audit record carry; raises ``DataplaneError`` naming the table (and the sample) that
    failed."""
    contract = _load_contract(name)
    by_table: dict[str, list[Path]] = {}
    for rel, path in staged:
        by_table.setdefault(_table_of(rel), []).append(path)
    wanted = getattr(contract, "table", None)
    tables = [wanted] if wanted else sorted(by_table) or [""]
    cap = contract_max_rows()
    report: dict[str, Any] = {"name": name, "sampled": False, "tables": {}}
    for table in tables:
        sample = read_contract_sample(by_table.get(table, []), cap)
        result = contract.validate(sample.frame)
        label = table or "(no data)"
        report["tables"][label] = sample.summary()
        report["sampled"] = report["sampled"] or sample.sampled
        if not result.passed:
            failures = "; ".join(f"{c.get('name')} ({c.get('observed')})" for c in result.errors)
            where = f"table {label!r}"
            if sample.sampled:
                where += f", checked the first {sample.rows} of {sample.total_rows} rows"
            raise DataplaneError(
                f"ingestion contract {name!r} failed on {where} (score {result.score}): {failures}"
            )
    if report["sampled"]:
        logger.info(
            "dataplane: ingestion contract %s checked a sample (%s=%d): %s",
            name,
            CONTRACT_MAX_ROWS_ENV,
            cap,
            report["tables"],
        )
    return report


def _record_revision(
    src: SourceDef, manifest: st.SnapshotManifest, store: st.DatasetStore, actor: str | None
) -> None:
    _record_revision_for(src.key, manifest, store, actor)


def _record_revision_for(
    key: str,
    manifest: st.SnapshotManifest,
    store: st.DatasetStore,
    actor: str | None,
    *,
    declare_asset: bool = True,
) -> bool:
    """Index one committed revision in ``dataset_revisions`` (idempotent on the revision).
    ``True`` when the row is new (or filled a link-only placeholder)."""
    from types import SimpleNamespace

    from examlops.data.data_assets import record_dataset_revision

    rev = SimpleNamespace(
        backend="dataplane",
        dataset=key,
        revision_id=manifest.revision,
        kind="dataplane",
        uri=store.uri(f"{key}/{manifest.pull_id}/_manifest.json"),
        schema_hash=manifest.schema_hash,
    )
    return bool(
        record_dataset_revision(
            rev,
            row_count=manifest.row_count,
            byte_count=manifest.byte_count,
            actor=_actor(actor),
            declare_asset=declare_asset,
        )
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


def _after_commit(
    src: SourceDef,
    store: st.DatasetStore,
    manifest: st.SnapshotManifest,
    *,
    pull_id: str,
    status: str,
    changed: bool,
    actor: str | None,
    started: float,
    secrets: list[str],
    full_reason: str | None,
    contract: dict[str, Any] | None = None,
) -> None:
    """Everything that follows the store commit (fix I3).

    The snapshot is committed — ``_latest`` moved — so nothing here may change the pull's
    outcome: each step is fail-open and logs a redacted warning. The final status goes first; if
    even that write fails the row stays ``committing`` and ``reap_interrupted_pulls`` settles it
    from the store. The revision row is recorded for ``unchanged`` pulls too (it is idempotent),
    so a row an earlier failure lost is written by the next pull.
    """
    details: dict[str, Any] = {
        "pull_id": pull_id,
        "revision": manifest.revision,
        "rows": manifest.row_count,
    }
    if full_reason:
        details["full_read"] = full_reason
    if contract is not None:
        details["contract"] = contract  # incl. whether the check read only a sample (I9)
    steps: list[tuple[str, Any]] = [
        (
            "final status",
            lambda: catalog.update_pull(
                pull_id,
                status=status,
                finished=True,
                revision=manifest.revision,
                row_count=manifest.row_count,
                byte_count=manifest.byte_count,
                watermark=manifest.watermark,
            ),
        ),
        ("revision index", lambda: _record_revision(src, manifest, store, actor)),
    ]
    if changed:
        steps.append(("announcement", lambda: _announce(src, manifest, pull_id)))
        steps.append(("audit", lambda: _audit("dataplane_pull_succeeded", src.key, details, actor)))
    steps.append(
        (
            "metrics",
            lambda: observe_pull(
                src.key,
                status,
                time.monotonic() - started,
                manifest.row_count,
                manifest.byte_count,
            ),
        )
    )
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — the commit stands; report, never re-raise
            logger.warning(
                "dataplane: pull %s of %s committed revision %s, but its %s step failed: %s",
                pull_id,
                src.key,
                manifest.revision[:12],
                label,
                redact(f"{type(exc).__name__}: {exc}", secrets=secrets),
            )


def _require_latest_unmoved(
    store: st.DatasetStore, key: str, start_ref: st.SnapshotRef | None
) -> None:
    """Refuse to commit when ``_latest`` is no longer the pointer this pull started from.

    Only a holder of the source's lock moves ``_latest``, so a move means another pull committed
    while this one ran — its lease lapsed without the heartbeat noticing. Committing now would
    replace that snapshot with one built on a stale parent. The comparison is on the *pointer*
    read at the start, not the parent manifest: a ``_latest`` naming a manifest that is gone
    (a store damaged out of band) yields no parent, and must not refuse every later pull."""
    try:
        current = st.resolve(store, key)
    except SnapshotNotFound:
        now: tuple[str, str] | None = None
    else:
        now = (current.revision, current.pull_id)
    then = (start_ref.revision, start_ref.pull_id) if start_ref is not None else None
    if now != then:
        raise PullInProgress(
            f"{key}'s _latest moved while this pull ran (another pull committed after this "
            "pull's lease lapsed); not committing"
        )


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
    """Pull one source and commit a snapshot, or report it ``unchanged``.

    Order of operations: take the source's lock and keep its lease alive (``_LeaseHeartbeat``)
    for the whole pull → decide incremental vs full (a parent whose ``spec_hash`` differs forces
    full) → record the pull ``running`` → read into a fresh stage dir (a connector's
    ``IncrementalInvalidated`` restarts the read as a full one, same pull id) → contract check →
    refuse to commit a pull whose lease was lost or whose row was reaped → ``committing`` →
    publish (the store commit) → fail-open bookkeeping (``_after_commit``) → release the lock.
    Only a failure *before* the commit marks the pull ``failed``.
    """
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
    lock_key = pull_lock_key(src.key)
    ttl = _lease_ttl_s(limits)
    if not coord.try_lock(lock_key, pull_id, ttl_s=ttl):
        raise PullInProgress(f"a pull of {src.key} is already running")
    lease = _LeaseHeartbeat(coord, lock_key, pull_id, ttl)
    started = time.monotonic()
    secret: str | None = None
    try:
        lease.start()
        try:
            store = store or st.store_from_env()
            try:
                start_ref: st.SnapshotRef | None = st.resolve(store, src.key)
            except SnapshotNotFound:
                start_ref = None
            try:
                parent: st.SnapshotManifest | None = (
                    st.read_manifest(store, start_ref) if start_ref is not None else None
                )
            except SnapshotNotFound:
                parent = None  # a dangling _latest: rebuild from scratch (a full pull)
            incremental = bool(src.spec.get("incremental")) and not full and parent is not None
            full_reason: str | None = None
            if incremental and parent is not None and parent.spec_hash != src.spec_hash:
                # The parent was read with another table/query/URL: extending it would mix two
                # datasets under one revision history.
                incremental = False
                full_reason = "the source spec changed since the parent snapshot"
                logger.info("dataplane: pull %s of %s is full: %s", pull_id, src.key, full_reason)
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

            def _read_attempt(since: dict[str, Any] | None) -> Any:
                """One complete read into a fresh stage dir.

                A transient failure mid-read discards this attempt's stage dir entirely (never
                merges partial parts from a failed attempt with a retried one) and lets
                ``retry_call`` start over from scratch.
                """
                tmpdir = tempfile.TemporaryDirectory(
                    prefix=f"{_STAGE_DIR_PREFIX}{pull_id}-", dir=stage_root
                )
                try:
                    writer = st.SnapshotWriter(Path(tmpdir.name), limits=limits)
                    for tb in connector.read(conn, src.spec, since, limits):
                        writer.write(tb)
                    staged = writer.close()
                    return staged, writer.watermark, tmpdir
                except Exception:
                    tmpdir.cleanup()
                    raise

            def _read(since: dict[str, Any] | None) -> Any:
                return retry_call(
                    lambda: _read_attempt(since),
                    retries=2,
                    retry_on=is_transient_network,
                    label=f"dataplane pull {src.key}",
                )

            try:
                staged, watermark, tmpdir = _read(
                    parent.watermark if incremental and parent else None
                )
            except IncrementalInvalidated as exc:
                # Upstream changed data the parent already holds: an append would duplicate or
                # keep it. Start over as a full read — fresh stage dir, same pull id.
                incremental = False
                full_reason = redact(str(exc), secrets=[secret] if secret else [])
                logger.info("dataplane: pull %s of %s is full: %s", pull_id, src.key, full_reason)
                staged, watermark, tmpdir = _read(None)
            try:
                contract_report: dict[str, Any] | None = None
                if src.contract:
                    contract_report = _check_contract(src.contract, staged)
                if lease.lost:
                    raise PullInProgress(
                        f"the lock lease on {src.key} was lost during the pull (another holder "
                        "took it); not committing"
                    )
                _require_latest_unmoved(store, src.key, start_ref)
                if not catalog.update_pull(
                    pull_id, status="committing", only_if_status=("running",)
                ):
                    raise PullInProgress(
                        f"pull {pull_id} of {src.key} was reaped as interrupted; not committing"
                    )
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
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}", secrets=[secret] if secret else [])
            try:
                # Conditional: a failure can never overwrite an outcome already recorded.
                catalog.update_pull(
                    pull_id,
                    status="failed",
                    finished=True,
                    error=message,
                    only_if_status=catalog.ACTIVE_PULL_STATUSES,
                )
            except Exception:
                pass
            _audit("dataplane_pull_failed", src.key, {"pull_id": pull_id, "error": message}, actor)
            observe_pull(src.key, "failed", time.monotonic() - started, 0, 0)
            if isinstance(exc, DataplaneError):
                raise type(exc)(message) from None
            raise DataplaneError(message) from None
        status = "succeeded" if changed else "unchanged"
        _after_commit(
            src,
            store,
            manifest,
            pull_id=pull_id,
            status=status,
            changed=changed,
            actor=actor,
            started=started,
            secrets=[secret] if secret else [],
            full_reason=full_reason,
            contract=contract_report,
        )
        return PullResult(
            pull_id,
            status,
            manifest.revision,
            manifest.row_count,
            manifest.byte_count,
            store.uri(f"{src.key}/{manifest.pull_id}/_manifest.json"),
            contract=contract_report,
        )
    finally:
        lease.stop()
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


def _committed_in_store(
    store: st.DatasetStore, key: str, pull_id: str
) -> st.SnapshotManifest | None:
    """The manifest of ``pull_id`` if the store holds it as a committed revision (its manifest and
    its ``_revisions`` pointer both exist); ``None`` if it definitely does not (no manifest, a
    corrupt one, no pointer). Any other error — the store unreachable — propagates: the caller
    must not decide a pull's fate on a read that did not happen."""
    manifest_key = f"{key}/{pull_id}/_manifest.json"
    try:
        manifest = st.read_manifest(store, st.SnapshotRef(key, "", pull_id, manifest_key))
    except (SnapshotNotFound, ValueError, KeyError, TypeError):
        return None
    return manifest if store.exists(f"{key}/_revisions/{manifest.revision}") else None


def _epoch(value: Any) -> float:
    """A manifest ``created_at`` or catalog timestamp as epoch seconds; ``0.0`` when unreadable
    (sorts as oldest)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
        except ValueError:
            return 0.0
    else:
        return 0.0
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _finish_commit(store: st.DatasetStore, key: str, manifest: st.SnapshotManifest) -> bool:
    """Point ``_latest`` at a snapshot a crashed pull committed (manifest + pointer) but never
    published, when it is newer than what ``_latest`` names now — by manifest ``created_at``, then
    pull id. A pull that committed after the crash is never rolled back. Caller holds the lock."""
    try:
        ref = st.resolve(store, key)
    except SnapshotNotFound:
        current: st.SnapshotManifest | None = None
    else:
        if ref.pull_id == manifest.pull_id:
            return False
        try:
            current = st.read_manifest(store, ref)
        except SnapshotNotFound:
            current = None  # a dangling `_latest`: any committed snapshot is better
    if current is not None and (_epoch(current.created_at), current.pull_id) >= (
        _epoch(manifest.created_at),
        manifest.pull_id,
    ):
        return False
    st.write_latest(store, key, manifest.revision, manifest.pull_id)
    logger.info("dataplane: finished the commit of pull %s of %s", manifest.pull_id, key)
    return True


def reap_interrupted_pulls(
    *, actor: str = "dataplane-reaper", store: st.DatasetStore | None = None
) -> list[str]:
    """Settle every ``running``/``queued``/``committing`` pull row whose process is gone.

    A dataplane process that is OOM-killed or crashes mid-pull leaves its catalog row active
    forever — nothing else ever transitions it. For each such row this tries the pull's own
    per-source coordinator lock (the same ``pull_lock_key`` and coordinator API ``run_pull`` uses)
    with a fresh holder and a short TTL:

    - if the lock is acquired, no live process owns that source's pull (a live pull keeps its
      lease renewed; a crashed holder's lease has since expired). A ``committing`` row whose
      snapshot the store already committed is marked ``succeeded`` with that revision, and the
      commit is finished (``_latest`` advanced) if nothing newer landed since. Every other row is
      marked ``failed``. Both writes are conditional on the row still being active, so a pull
      that finished between the listing and the lock keeps its real outcome. A ``committing`` row
      whose store cannot be read right now is left for the next sweep — never failed on a read
      that did not happen.
    - if the lock is still held, a live pull — or a crashed holder whose lease has not yet
      expired — is left untouched; the lease TTL is what bounds how long a crashed pull can look
      active.

    Called from the service lifespan at startup and at the start of each scheduler tick.
    """
    coord = get_coordinator()
    reaped: list[str] = []
    for row in catalog.list_active_pulls():
        source = st.source_key(row["project"], row["source"])
        lock_key = pull_lock_key(source)
        holder = f"{actor}:{row['id']}"
        if not coord.try_lock(lock_key, holder, ttl_s=_REAP_LOCK_TTL_S):
            continue  # a live process, or an unexpired crashed holder, still owns this pull
        try:
            committed = None
            if row["status"] == "committing":
                try:
                    store = store or st.store_from_env()
                    committed = _committed_in_store(store, source, row["id"])
                except Exception as exc:  # noqa: BLE001 — transient: decide on the next sweep
                    logger.warning(
                        "dataplane: cannot read the store to settle committing pull %s; "
                        "retrying on the next sweep: %s",
                        row["id"],
                        redact(f"{type(exc).__name__}: {exc}"),
                    )
                    continue
            if committed is not None and store is not None:
                _finish_commit(store, source, committed)
                changed = catalog.update_pull(
                    row["id"],
                    status="succeeded",
                    finished=True,
                    revision=committed.revision,
                    row_count=committed.row_count,
                    byte_count=committed.byte_count,
                    watermark=committed.watermark,
                    only_if_status=("committing",),
                )
                if changed:
                    try:
                        _record_revision_for(source, committed, store, actor)
                    except Exception:  # noqa: BLE001 — the next pull of the source heals it
                        logger.warning("dataplane: could not index revision of %s", row["id"])
            else:
                changed = catalog.update_pull(
                    row["id"],
                    status="failed",
                    finished=True,
                    error="interrupted: the process running this pull stopped before it finished",
                    only_if_status=catalog.ACTIVE_PULL_STATUSES,
                )
        except Exception:  # noqa: BLE001 — one bad row must not abandon the rest of the sweep
            logger.warning("dataplane: could not reap pull %s", row["id"], exc_info=True)
            continue
        finally:
            coord.unlock(lock_key, holder)
        if changed:
            reaped.append(row["id"])
    if reaped:
        logger.info("dataplane: reaped %d interrupted pull(s): %s", len(reaped), ", ".join(reaped))
    return reaped


def _has_committed_snapshots(store: st.DatasetStore, key: str) -> bool:
    return store.exists(f"{key}/_latest") or bool(store.ls_files(f"{key}/_revisions"))


def prune_source(
    project: str,
    name: str,
    *,
    keep: int,
    pinned: set[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    store: st.DatasetStore | None = None,
    actor: str | None = None,
) -> list[str]:
    """Prune one source's old snapshots — the one entry point for the CLI and any service path.

    - Holds the source's pull lock (``pull_lock_key``, fresh holder, renewed by the heartbeat)
      for the whole prune, so it never runs beside a pull of the same source; a held lock raises
      ``PullInProgress``.
    - ``pinned`` defaults to every revision a training run linked (``dataset_revisions`` rows
      with an ``mlflow_run_id``); those, the newest ``keep`` and ``_latest`` survive.
    - Refuses when the store holds committed revisions but the catalog has no
      ``dataset_revisions`` row for the source: that is a lost catalog, and pinned protection
      would be blind. Run ``exa dataplane catalog-rebuild`` first, or pass ``force``.

    Returns the removed pull ids.
    """
    from examlops.data.data_assets import get_dataset_revisions

    key = st.source_key(project, name)
    store = store or st.store_from_env()
    coord = get_coordinator()
    lock_key = pull_lock_key(key)
    holder = f"prune:{catalog.new_pull_id()}"
    if not coord.try_lock(lock_key, holder, ttl_s=_PRUNE_LOCK_TTL_S):
        raise PullInProgress(f"a pull of {key} is running; prune it after the pull finishes")
    lease = _LeaseHeartbeat(coord, lock_key, holder, _PRUNE_LOCK_TTL_S)
    try:
        lease.start()
        rows = get_dataset_revisions(key, backend="dataplane")
        if not rows and not force and _has_committed_snapshots(store, key):
            raise DataplaneError(
                f"the store has committed snapshots of {key} but the catalog has no revision rows "
                "for it — platform.db looks lost or restored, so revisions pinned by training runs "
                "cannot be protected. Run `exa dataplane catalog-rebuild` first, or pass --force"
            )
        if pinned is None:
            pinned = {r["revision_id"] for r in rows if r.get("mlflow_run_id")}
        removed = st.prune(store, key, keep=keep, pinned=set(pinned), dry_run=dry_run)
    finally:
        lease.stop()
        coord.unlock(lock_key, holder)
    if removed and not dry_run:
        _audit(
            "dataplane_snapshots_pruned",
            key,
            {"removed": removed, "keep": keep, "forced": force},
            actor,
        )
    return removed


def _sql_timestamp(value: Any) -> str | None:
    """``YYYY-MM-DD HH:MM:SS`` UTC — the form ``CURRENT_TIMESTAMP`` writes — or ``None`` for
    anything that is not a readable timestamp (a hand-edited manifest must not break a rebuild)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _pull_started_at(pull_id: str, manifest: st.SnapshotManifest) -> str | None:
    """When a pull started: its id's nanosecond prefix, else the manifest's ``created_at``."""
    if st._looks_like_pull_id(pull_id):
        try:
            return _sql_timestamp(datetime.fromtimestamp(int(pull_id[:16], 16) / 1e9, UTC))
        except (OverflowError, OSError, ValueError):
            pass
    return _sql_timestamp(manifest.created_at)


def _declare_dataset_changed(key: str, actor: str | None) -> None:
    """Advance the dataset's asset once (downstream models go stale) — fail-open, like
    ``data_assets._declare_dataset_asset``."""
    try:
        from examlops import assets

        assets.mark_source_changed(key, actor=actor)
    except Exception as exc:  # noqa: BLE001 — the asset graph is a derived view of the catalog
        logger.warning("dataplane: could not advance the asset of %s: %s", key, redact(str(exc)))


def rebuild_catalog(
    *, store: st.DatasetStore | None = None, dry_run: bool = False, actor: str | None = None
) -> dict[str, Any]:
    """Re-create the catalog's index of committed snapshots by walking the store (idempotent).

    Visits every ``<project|_global>/<source>/`` prefix, every pull dir with a manifest and the
    ``_revisions`` pointers. Every committed snapshot (manifest + revision pointer) gets its
    ``dataplane_pulls`` row back (status ``succeeded``, revision, counts, ``finished_at`` from the
    manifest) and its ``dataset_revisions`` row — every committed revision, not just the latest.
    Existing rows are never changed.

    Source *definitions* cannot be rebuilt: a manifest carries the connector, the connection name
    and the spec's hash, not the spec. Sources with snapshots but no catalog definition are
    reported under ``sources_to_register``. Which training run used a revision
    (``mlflow_run_id``) is not in the store either; the next run on that revision links itself.

    Re-indexing history is not news: revisions are recorded without advancing the dataset's
    asset, and the asset is advanced at most once per source — only when a newly indexed revision
    is newer (manifest ``created_at``) than every revision the catalog already had. A pull dir
    that cannot be restored is skipped with a warning (``skipped``), never aborting the rebuild.
    """
    from examlops.data.data_assets import get_dataset_revisions

    store = store or st.store_from_env()
    who = _actor(actor)
    report: dict[str, Any] = {
        "sources": 0,
        "snapshots": 0,
        "revisions": 0,
        "pulls_restored": 0,
        "uncommitted": 0,
        "unreadable": 0,
        "skipped": 0,
        "sources_to_register": [],
        "dry_run": dry_run,
    }
    for top in store.ls_dirs(""):
        if top == "_global":
            project = ""
        else:
            try:
                project = validate_name(top, "project")
            except SpecError:
                continue  # not a source prefix
        for name in store.ls_dirs(top):
            try:
                key = st.source_key(project, name)
            except SpecError:
                continue
            pointers = set(store.ls_files(f"{key}/_revisions"))
            committed: list[tuple[str, st.SnapshotManifest]] = []
            for pid in store.ls_dirs(key):
                if pid.startswith("_"):
                    continue
                manifest_key = f"{key}/{pid}/_manifest.json"
                if not store.exists(manifest_key):
                    continue  # a pull that never reached its manifest
                try:
                    manifest = st.read_manifest(store, st.SnapshotRef(key, "", pid, manifest_key))
                except (SnapshotNotFound, ValueError, KeyError, TypeError):
                    report["unreadable"] += 1
                    continue
                if manifest.revision not in pointers:
                    report["uncommitted"] += 1  # crashed between its manifest and its pointer
                    continue
                committed.append((pid, manifest))
            if not committed:
                continue
            committed.sort(key=lambda pm: pm[0])  # oldest first: catalog ids ascend in time
            report["sources"] += 1
            report["snapshots"] += len(committed)
            revisions = {m.revision for _, m in committed}
            report["revisions"] += len(revisions)
            newest = committed[-1][1]
            if catalog.get_source(name, project) is None:
                report["sources_to_register"].append(
                    {
                        "project": project,
                        "name": name,
                        "connector": newest.connector,
                        "connection": newest.connection,
                        "spec_hash": newest.spec_hash,
                    }
                )
            if dry_run:
                report["pulls_restored"] += sum(
                    1 for pid, _ in committed if catalog.get_pull(pid) is None
                )
                continue
            # When each revision was last committed, for the "is anything actually newer" test.
            born: dict[str, float] = {}
            for _pid, m in committed:
                born[m.revision] = max(born.get(m.revision, 0.0), _epoch(m.created_at))
            indexed = {
                r["revision_id"]: born.get(r["revision_id"], _epoch(r.get("created_at")))
                for r in get_dataset_revisions(key, backend="dataplane")
            }
            recorded: set[str] = set()
            fresh: set[str] = set()
            for pid, manifest in committed:
                try:
                    if catalog.restore_pull(
                        pid,
                        project,
                        name,
                        revision=manifest.revision,
                        parent_revision=manifest.parent_revision,
                        row_count=manifest.row_count,
                        byte_count=manifest.byte_count,
                        watermark=manifest.watermark,
                        started_at=_pull_started_at(pid, manifest),
                        finished_at=_sql_timestamp(manifest.created_at),
                        actor=who,
                    ):
                        report["pulls_restored"] += 1
                    if manifest.revision not in recorded:
                        recorded.add(manifest.revision)
                        new = _record_revision_for(key, manifest, store, who, declare_asset=False)
                        if new and manifest.revision not in indexed:
                            fresh.add(manifest.revision)
                except Exception as exc:  # noqa: BLE001 — one bad dir must not abort the rest
                    report["skipped"] += 1
                    logger.warning(
                        "dataplane: catalog-rebuild skipped pull dir %s of %s: %s",
                        pid,
                        key,
                        redact(f"{type(exc).__name__}: {exc}"),
                    )
            newest_fresh = max((born[r] for r in fresh), default=None)
            newest_known = max(indexed.values(), default=None)
            if newest_fresh is not None and (newest_known is None or newest_fresh > newest_known):
                _declare_dataset_changed(key, who)
    return report


def cleanup_stale_stage_dirs(*, root: Path | None = None, cutoff: float | None = None) -> int:
    """Remove leftover pull stage dirs from a previous process (task 22a).

    A crashed/OOM-killed pull's stage dir (``dp-<pull_id>-*``, created by ``run_pull``'s
    ``TemporaryDirectory``) is never cleaned up by anything else once its owning process is gone.
    At service startup, every ``dp-*`` entry directly under ``root`` (default
    ``tempfile.gettempdir()`` — compose points this at ``/stage``, a volume private to this
    service, via ``TMPDIR``) whose mtime is older than ``cutoff`` (default: this process's own
    start time, ``_PROCESS_START``, captured once at import) belongs to an earlier process: this
    process's own pulls only ever create stage dirs after startup has run. Symlinks are never
    followed — only real directories are matched and removed.

    ``cutoff`` is exposed for tests; production callers should never pass it.
    """
    base = root or Path(tempfile.gettempdir())
    if not base.is_dir():
        return 0
    cutoff = _PROCESS_START if cutoff is None else cutoff
    removed = 0
    for entry in base.iterdir():
        if not entry.name.startswith(_STAGE_DIR_PREFIX) or entry.is_symlink():
            continue
        try:
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            logger.warning("dataplane: could not remove stale stage dir %s: %s", entry, exc)
    if removed:
        logger.info("dataplane: removed %d stale stage dir(s) under %s", removed, base)
    return removed


__all__ = [
    "ContractSample",
    "PullResult",
    "SourceDef",
    "cleanup_stale_stage_dirs",
    "contract_max_rows",
    "define_source",
    "get_source_def",
    "list_source_defs",
    "preview",
    "prune_source",
    "pull_lock_key",
    "read_contract_sample",
    "reap_interrupted_pulls",
    "rebuild_catalog",
    "remove_source",
    "run_pull",
    "test_source",
]
