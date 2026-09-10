"""The instance-data format stamp and the compatibility rule (ADR 0128).

Every datastore carries a small stamp in ``platform_meta``:

* ``instance_id`` — a UUID minted the first time an instance's data is opened. It identifies the
  *data*, not the install, so a backup can say which instance it belongs to and a restore onto a
  different instance is visible.
* ``data_format`` — the highest migration (:mod:`examlops.lifecycle.migrations`) applied to it.
* ``min_reader_format`` — the lowest code data-format that may still open it. Only a *breaking*
  migration raises it, which is what keeps a rollback to an older release safe after an ordinary
  (additive) upgrade and refuses one after a contract change.
* ``created_with`` / ``last_opened_with`` — the ExaMLOps versions that created and last opened it.

The rule, like SQLite's read/write version bytes: code with data format ``C`` may open data
stamped ``(F, R)`` iff ``C >= R``. ``C > F`` means migrations are pending; ``C < F`` (with
``C >= R``) means an older release is running on newer, still-compatible data — a rollback.

The stamp lives *in* the datastore, so it moves with the data through backup, restore and a
SQLite→Postgres switch, and needs nothing beyond the ``get_db`` connection every helper uses.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from examlops.lifecycle import migrations as _mig

log = logging.getLogger(__name__)

#: The data format this code reads and writes (the registry is read at call time everywhere
#: else, so a test can swap it; this constant is for display).
CODE_DATA_FORMAT: int = _mig.code_format()

#: Escape hatch for a deliberate, understood downgrade onto data an older release cannot read.
ALLOW_INCOMPATIBLE_ENV = "EXAMLOPS_ALLOW_INCOMPATIBLE_DATA"

BASELINE_FORMAT = 1

# Status vocabulary (plain strings so reports stay JSON-serialisable).
UNSTAMPED = "unstamped"
CURRENT = "current"
UPGRADE_AVAILABLE = "upgrade_available"  # pending migrations are all online — applied on open
UPGRADE_REQUIRED = "upgrade_required"  # an offline migration is pending — run `exa upgrade apply`
NEWER_COMPATIBLE = "newer_compatible"  # written by a newer release this code can still read
TOO_NEW = "too_new"  # written by a newer release this code must not read

_META_DDL = (
    "CREATE TABLE IF NOT EXISTS platform_meta ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS platform_upgrades ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,"
    " from_format INTEGER, to_format INTEGER, migration TEXT, from_version TEXT,"
    " to_version TEXT, actor TEXT, backup_id TEXT, detail TEXT)",
)


class IncompatibleDataError(RuntimeError):
    """The datastore was written by a release this code cannot safely read."""


def code_version() -> str:
    """The installed ExaMLOps version (``dev`` for a source checkout without metadata)."""
    try:
        from importlib.metadata import version

        return version("examlops")
    except Exception:  # noqa: BLE001 — metadata is absent in some bare checkouts
        return "dev"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "examlops"


@dataclass
class Stamp:
    instance_id: str
    data_format: int
    min_reader_format: int
    created_with: str
    created_at: str
    last_opened_with: str
    last_opened_at: str
    adopted_from: str  # "fresh" (created by this lineage) or "legacy" (pre-0128 data adopted)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Compatibility:
    status: str
    ok: bool  # may this code open the data?
    code_format: int
    data_format: int | None
    min_reader_format: int | None
    message: str
    action: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_tables(conn: Any) -> None:
    for ddl in _META_DDL:
        conn.execute(ddl)


def _meta(conn: Any) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT key, value FROM platform_meta").fetchall()
    except Exception:  # noqa: BLE001 — table absent: the data predates the stamp
        return {}
    return {str(r[0]): str(r[1]) for r in rows}


def read_stamp(conn: Any) -> Stamp | None:
    """The stamp, or ``None`` when the datastore has never been stamped."""
    m = _meta(conn)
    if "data_format" not in m:
        return None
    return Stamp(
        instance_id=m.get("instance_id", ""),
        data_format=int(m["data_format"]),
        min_reader_format=int(m.get("min_reader_format", BASELINE_FORMAT)),
        created_with=m.get("created_with", ""),
        created_at=m.get("created_at", ""),
        last_opened_with=m.get("last_opened_with", ""),
        last_opened_at=m.get("last_opened_at", ""),
        adopted_from=m.get("adopted_from", "fresh"),
    )


def _pending_dicts(pend: Sequence[_mig.Migration]) -> list[dict[str, Any]]:
    return [
        {
            "version": m.version,
            "name": m.name,
            "description": m.description,
            "online": m.online,
            "breaking": m.breaking,
        }
        for m in pend
    ]


def evaluate(
    data_format: int | None,
    min_reader_format: int | None,
    *,
    registry: Sequence[_mig.Migration] | None = None,
) -> Compatibility:
    """Decide whether code (whose format is ``registry``'s) may open data stamped this way."""
    registry = _mig.MIGRATIONS if registry is None else registry
    code = _mig.code_format(registry)
    if data_format is None:
        return Compatibility(
            UNSTAMPED,
            True,
            code,
            None,
            None,
            "No data-format stamp yet — it is written the first time the platform opens the "
            "datastore (existing data is adopted at the baseline format).",
        )
    reader = min_reader_format if min_reader_format is not None else BASELINE_FORMAT
    if code < reader:
        return Compatibility(
            TOO_NEW,
            False,
            code,
            data_format,
            reader,
            f"This data was written by a newer ExaMLOps (data format {data_format}, readable only "
            f"by format ≥ {reader}); this release understands format {code}.",
            "Install the newer release again, or restore a backup taken before its upgrade.",
        )
    if code < data_format:
        return Compatibility(
            NEWER_COMPATIBLE,
            True,
            code,
            data_format,
            reader,
            f"Data format {data_format} was written by a newer release; this release (format "
            f"{code}) can still read it — a rollback within the compatibility window.",
        )
    pend = _mig.pending(data_format, registry)
    if not pend:
        return Compatibility(
            CURRENT, True, code, data_format, reader, f"Data is at format {data_format}."
        )
    if all(m.online for m in pend):
        return Compatibility(
            UPGRADE_AVAILABLE,
            True,
            code,
            data_format,
            reader,
            f"{len(pend)} online migration(s) pending; they apply automatically when the "
            "platform next opens the datastore.",
            "exa upgrade apply   (or just start any ExaMLOps process)",
            _pending_dicts(pend),
        )
    return Compatibility(
        UPGRADE_REQUIRED,
        True,
        code,
        data_format,
        reader,
        f"{len(pend)} migration(s) pending, including offline ones that need a backup and an "
        "explicit upgrade.",
        "exa upgrade apply",
        _pending_dicts(pend),
    )


def evaluate_stamp(stamp: Stamp | None) -> Compatibility:
    if stamp is None:
        return evaluate(None, None)
    return evaluate(stamp.data_format, stamp.min_reader_format)


def evaluate_manifest(manifest: dict[str, Any]) -> Compatibility:
    """Compatibility of a backup bundle's data with this code.

    A bundle from before ADR 0128 carries no format; its data is what format 1 means (the
    baseline adopts any existing schema), so it is evaluated as ``(1, 1)``.
    """
    fmt = manifest.get("data_format")
    reader = manifest.get("min_reader_format")
    return evaluate(
        int(fmt) if fmt is not None else BASELINE_FORMAT,
        int(reader) if reader is not None else BASELINE_FORMAT,
    )


def _set(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO platform_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )


def _set_if_absent(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO platform_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (key, value, _now()),
    )


def _record(
    conn: Any,
    kind: str,
    *,
    from_format: int | None,
    to_format: int | None,
    migration: str | None = None,
    from_version: str | None = None,
    backup_id: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO platform_upgrades (ts, kind, from_format, to_format, migration, "
        "from_version, to_version, actor, backup_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            _now(),
            kind,
            from_format,
            to_format,
            migration,
            from_version,
            code_version(),
            _actor(),
            backup_id,
            detail,
        ),
    )


def _has_prior_data(conn: Any) -> bool:
    try:
        return conn.execute("SELECT 1 FROM audit_events LIMIT 1").fetchone() is not None
    except Exception:  # noqa: BLE001 — no audit table means nothing was ever recorded
        return False


def stamp_if_needed(conn: Any) -> Stamp:
    """Write the initial stamp when there is none; return the (possibly just written) stamp.

    Data that already holds records is *adopted* at the baseline format — every pre-0128 schema
    is by definition what format 1 means — so its pending migrations still run. An empty
    datastore was just created by this code and is stamped at the code's own format.
    """
    ensure_tables(conn)
    existing = read_stamp(conn)
    if existing is not None:
        return existing
    legacy = _has_prior_data(conn)
    fmt = BASELINE_FORMAT if legacy else _mig.code_format(_mig.MIGRATIONS)
    reader = BASELINE_FORMAT if legacy else _mig.min_reader_after(BASELINE_FORMAT, _mig.MIGRATIONS)
    version = code_version()
    now = _now()
    iid = str(uuid.uuid4())
    # ON CONFLICT DO NOTHING: two processes opening a fresh datastore at once stamp it once.
    for key, value in (
        ("instance_id", iid),
        ("data_format", str(fmt)),
        ("min_reader_format", str(reader)),
        ("created_with", version if not legacy else "pre-0.52 (adopted)"),
        ("created_at", now),
        ("adopted_from", "legacy" if legacy else "fresh"),
        ("last_opened_with", version),
        ("last_opened_at", now),
    ):
        _set_if_absent(conn, key, value)
    stamp = read_stamp(conn)
    assert stamp is not None  # just written
    if stamp.instance_id == iid:  # this process wrote the stamp, so it records the event
        _record(conn, "adopt" if legacy else "create", from_format=None, to_format=fmt)
    return stamp


def apply_migrations(
    conn: Any,
    to_apply: Sequence[_mig.Migration],
    *,
    kind: str,
    backup_id: str | None = None,
) -> list[str]:
    """Apply ``to_apply`` in order on ``conn``, advancing the stamp with a compare-and-set.

    The compare-and-set makes concurrent openers safe: if another process advanced the format
    first, this one stops (the migrations are idempotent, so a repeated ``apply`` is harmless).
    """
    applied: list[str] = []
    stamp = read_stamp(conn)
    if stamp is None:
        stamp = stamp_if_needed(conn)
    current, reader = stamp.data_format, stamp.min_reader_format
    for m in to_apply:
        if m.version != current + 1:
            break
        m.apply(conn)
        cur = conn.execute(
            "UPDATE platform_meta SET value = ?, updated_at = ? "
            "WHERE key = 'data_format' AND value = ?",
            (str(m.version), _now(), str(current)),
        )
        if getattr(cur, "rowcount", 1) == 0:
            break  # another process got there first
        if m.breaking:
            reader = _mig.min_reader_after(reader, [m])
            _set(conn, "min_reader_format", str(reader))
        _record(
            conn,
            kind,
            from_format=current,
            to_format=m.version,
            migration=m.name,
            from_version=stamp.last_opened_with,
            backup_id=backup_id,
        )
        applied.append(m.name)
        current = m.version
    return applied


def on_schema_ready(conn: Any) -> None:
    """Called by :func:`examlops.platform_db.init_db` once the additive schema exists.

    Stamps the datastore, refuses data this code must not read, applies pending *online*
    migrations, and records which release last opened it. Fail-closed only on genuine
    incompatibility; any other problem here is logged and never blocks the platform.
    """
    try:
        stamp = stamp_if_needed(conn)
        compat = evaluate_stamp(stamp)
        if compat.status == TOO_NEW:
            if os.getenv(ALLOW_INCOMPATIBLE_ENV, "").strip().lower() not in {"1", "true", "yes"}:
                raise IncompatibleDataError(
                    f"{compat.message} {compat.action} "
                    f"(set {ALLOW_INCOMPATIBLE_ENV}=1 to override at your own risk)"
                )
            log.warning("opening incompatible data by override: %s", compat.message)
        online: list[_mig.Migration] = []
        for m in _mig.pending(stamp.data_format, _mig.MIGRATIONS):
            if not m.online:
                break
            online.append(m)
        if online:
            apply_migrations(conn, online, kind="online")
        elif compat.status == UPGRADE_REQUIRED:
            log.warning("%s Run: %s", compat.message, compat.action)
        version = code_version()
        if stamp.last_opened_with != version:
            _set(conn, "last_opened_with", version)
            _set(conn, "last_opened_at", _now())
    except IncompatibleDataError:
        raise
    except Exception as exc:  # noqa: BLE001 — the stamp must never take the platform down
        log.warning("data-format stamp skipped: %s", exc)


def history(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first record of every create / adopt / migration / restore on this datastore."""
    try:
        rows = conn.execute(
            "SELECT ts, kind, from_format, to_format, migration, from_version, to_version, "
            "actor, backup_id, detail FROM platform_upgrades ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    except Exception:  # noqa: BLE001 — never stamped yet
        return []
    cols = (
        "ts",
        "kind",
        "from_format",
        "to_format",
        "migration",
        "from_version",
        "to_version",
        "actor",
        "backup_id",
        "detail",
    )
    return [dict(zip(cols, r, strict=True)) for r in rows]
