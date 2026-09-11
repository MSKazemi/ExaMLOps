"""Dataset store for dataplane snapshots (ADR 0130 §7).

Layout under the store root::

    <project|_global>/<source>/<pull_id>/<table>/part-NNNNN.parquet
    <project|_global>/<source>/<pull_id>/_manifest.json
    <project|_global>/<source>/_revisions/<revision>     -> pull_id
    <project|_global>/<source>/_latest                   -> {"revision", "pull_id"}  (written LAST)

The ``_latest`` write is the commit. The store describes itself, so a node without platform.db
can resolve and download a revision; the catalog tables are only an index.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from examlops.data.content_hash import file_digest, revision_id, schema_hash, schema_part
from examlops.dataplane.safety import validate_name
from examlops.dataplane.types import (
    LimitExceeded,
    Limits,
    SnapshotIntegrityError,
    SnapshotNotFound,
    SpecError,
    TableBatch,
)

_ORPHAN_AGE_S = 3600  # an uncommitted pull dir whose newest object is older than this is garbage
# A revision id is `revision_id()`'s sha256 hex digest — nothing else may name a store key.
_REVISION_RE = re.compile(r"[0-9a-f]{64}")


def source_key(project: str, name: str) -> str:
    validate_name(name, "source name")
    if project:
        validate_name(project, "project")
    return f"{project or '_global'}/{name}"


class DatasetStore:
    def __init__(self, fs: Any, root: str, *, uri_prefix: str) -> None:
        self.fs = fs
        self.root = root.rstrip("/")
        self.uri_prefix = uri_prefix.rstrip("/")

    @classmethod
    def from_url(cls, url: str, **storage_options: Any) -> DatasetStore:
        # s3:// goes through pyarrow's native S3 filesystem (examlops.dataplane.s3), not s3fs.
        from examlops.dataplane.s3 import url_to_fs

        fs, root = url_to_fs(url, **storage_options)
        return cls(fs, root, uri_prefix=url)

    def _path(self, key: str) -> str:
        return f"{self.root}/{key}" if key else self.root

    def uri(self, key: str) -> str:
        return f"{self.uri_prefix}/{key}"

    def exists(self, key: str) -> bool:
        return bool(self.fs.exists(self._path(key)))

    def read_text(self, key: str) -> str:
        with self.fs.open(self._path(key), "r") as fh:
            return str(fh.read())

    def write_text(self, key: str, text: str) -> None:
        self.fs.makedirs(self._path(str(PurePosixPath(key).parent)), exist_ok=True)
        with self.fs.open(self._path(key), "w") as fh:
            fh.write(text)

    def put_file(self, local: Path, key: str) -> None:
        self.fs.makedirs(self._path(str(PurePosixPath(key).parent)), exist_ok=True)
        self.fs.put_file(str(local), self._path(key))

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.fs.get_file(self._path(key), str(local))

    def ls_dirs(self, key: str) -> list[str]:
        if not self.exists(key):
            return []
        out = []
        for entry in self.fs.ls(self._path(key), detail=True):
            if entry.get("type") == "directory":
                out.append(PurePosixPath(entry["name"]).name)
        return sorted(out)

    def ls_files(self, key: str) -> list[str]:
        """Names of the plain files directly under ``key`` (``_revisions/<rev>`` pointers)."""
        if not self.exists(key):
            return []
        return sorted(
            PurePosixPath(entry["name"]).name
            for entry in self.fs.ls(self._path(key), detail=True)
            if entry.get("type") == "file"
        )

    def newest_mtime(self, key: str) -> float | None:
        """Epoch seconds of the most recently modified object under ``key``, or ``None`` when the
        listing carries no modification time for any of them (or there are none)."""
        if not self.exists(key):
            return None
        found = self.fs.find(self._path(key), detail=True)
        infos = found.values() if isinstance(found, dict) else ()
        times = [t for t in (_mtime_s(info) for info in infos) if t is not None]
        return max(times) if times else None

    def rm(self, key: str) -> None:
        if self.exists(key):
            self.fs.rm(self._path(key), recursive=True)


def _mtime_s(info: dict[str, Any]) -> float | None:
    """An fsspec listing entry's modification time as epoch seconds.

    fsspec's local filesystem reports ``mtime`` as a float, pyarrow's (S3 behind
    ``ArrowFSWrapper``) as a ``datetime``, s3fs as ``LastModified``; an object store has no
    separate creation time, and each object is written once, so ``created`` is the same instant.
    """
    for name in ("mtime", "LastModified", "last_modified", "created"):
        value = info.get(name)
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def store_from_env() -> DatasetStore:
    """``EXAMLOPS_DATAPLANE_STORE_URL`` wins; otherwise ``s3://<EXAMLOPS_DATA_BUCKET>/dataplane``
    on the dataset store (``EXAMLOPS_DATA_S3_*``), falling back to the platform MinIO.

    The store is operator-configured and trusted, unlike a connector's source: its endpoint is not
    egress-checked (an internal name such as ``minio`` is the normal case), and with no explicit
    key/secret it is **not** anonymous — pyarrow's default AWS credential chain (env, ``~/.aws``,
    IRSA, instance role) serves it (``anon=False``). A source never gets that chain."""
    url = os.getenv("EXAMLOPS_DATAPLANE_STORE_URL", "").strip()
    if url and not url.startswith("s3://"):
        return DatasetStore.from_url(url)
    bucket = os.getenv("EXAMLOPS_DATA_BUCKET", "examlops-data")
    url = url or f"s3://{bucket}/dataplane"
    endpoint = os.getenv("EXAMLOPS_DATA_S3_ENDPOINT") or os.getenv("MLFLOW_S3_ENDPOINT_URL")
    key = os.getenv("EXAMLOPS_DATA_S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
    secret = os.getenv("EXAMLOPS_DATA_S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
    opts: dict[str, Any] = {"key": key, "secret": secret, "anon": False}
    if endpoint:
        opts["client_kwargs"] = {"endpoint_url": endpoint}
    return DatasetStore.from_url(url, **opts)


@dataclass(frozen=True)
class ManifestFile:
    path: str  # relative path inside the snapshot, e.g. "jobs/part-00000.parquet"
    object: str  # store key holding the bytes (may live in a parent pull's dir)
    sha256: str
    bytes: int
    rows: int
    schema: str


@dataclass(frozen=True)
class SnapshotManifest:
    source: str
    connector: str
    connection: str | None
    spec_hash: str
    revision: str
    schema_hash: str
    files: tuple[ManifestFile, ...]
    tables: tuple[str, ...]
    row_count: int
    byte_count: int
    watermark: dict[str, Any] = field(default_factory=dict)
    parent_revision: str | None = None
    pull_id: str = ""
    created_at: str = ""
    examlops_version: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, sort_keys=True, indent=2, default=str)

    @classmethod
    def from_json(cls, text: str) -> SnapshotManifest:
        d = json.loads(text)
        d["files"] = tuple(ManifestFile(**f) for f in d["files"])
        d["tables"] = tuple(d["tables"])
        return cls(**d)


@dataclass(frozen=True)
class SnapshotRef:
    source: str
    revision: str
    pull_id: str
    manifest_key: str


class SnapshotWriter:
    """Streams TableBatches into Parquet parts under ``stage_dir``, enforcing limits.

    A batch whose schema differs from the open part is cast to it when possible (e.g. an all-null
    column); otherwise a new part starts with the new schema — each part records its own schema.
    """

    def __init__(self, stage_dir: Path, *, limits: Limits, part_rows: int = 1_000_000) -> None:
        self.stage_dir = Path(stage_dir)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.limits = limits
        self.part_rows = part_rows
        self.rows = 0
        self.bytes = 0
        self.watermark: dict[str, Any] = {}
        self._open: dict[
            str, tuple[Any, Any, int, Path]
        ] = {}  # table -> (writer, schema, rows, path)
        self._next: dict[str, int] = {}
        self._done: list[tuple[str, Path]] = []
        self._started = time.monotonic()

    def _roll(self, table: str) -> None:
        if table in self._open:
            writer, _schema, _rows, path = self._open.pop(table)
            writer.close()
            self.bytes += path.stat().st_size
            self._done.append((str(PurePosixPath(table) / path.name), path))

    def _start(self, table: str, schema: Any) -> None:
        import pyarrow.parquet as pq

        idx = self._next.get(table, 0)
        self._next[table] = idx + 1
        path = self.stage_dir / table / f"part-{idx:05d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._open[table] = (pq.ParquetWriter(path, schema), schema, 0, path)

    def write(self, tb: TableBatch) -> None:
        validate_name(tb.table, "table name")
        batch = tb.batch
        if batch.num_rows == 0:
            if tb.watermark:
                self.watermark = dict(tb.watermark)
            return
        if self.limits.max_rows is not None and self.rows + batch.num_rows > self.limits.max_rows:
            raise LimitExceeded(f"source exceeded max_rows={self.limits.max_rows}")
        if (
            self.limits.max_seconds is not None
            and time.monotonic() - self._started > self.limits.max_seconds
        ):
            raise LimitExceeded(f"source exceeded max_seconds={self.limits.max_seconds}")
        current = self._open.get(tb.table)
        if current is not None and not batch.schema.equals(current[1]):
            try:
                batch = batch.cast(current[1])
            except Exception:
                self._roll(tb.table)
                current = None
        if current is None or current[2] >= self.part_rows:
            if current is not None:
                self._roll(tb.table)
            self._start(tb.table, batch.schema)
        writer, schema, rows, path = self._open[tb.table]
        writer.write_batch(batch)
        self._open[tb.table] = (writer, schema, rows + batch.num_rows, path)
        self.rows += batch.num_rows
        pending = sum(p.stat().st_size for _, _, _, p in self._open.values() if p.exists())
        if self.limits.max_bytes is not None and self.bytes + pending > self.limits.max_bytes:
            raise LimitExceeded(f"source exceeded max_bytes={self.limits.max_bytes}")
        if tb.watermark:
            self.watermark = dict(tb.watermark)

    def close(self) -> list[tuple[str, Path]]:
        for table in list(self._open):
            self._roll(table)
        return sorted(self._done, key=lambda rp: PurePosixPath(rp[0]).parts)


def _revision_of(files: Iterable[ManifestFile]) -> tuple[str, str]:
    """``(revision, schema_hash)`` for a snapshot's files — THE formula, shared by ``publish``
    (which names a snapshot) and ``materialize`` (which checks a stored one still has that name).
    ``files`` must be in sorted path order, as a manifest stores them."""
    ordered = list(files)
    sh = schema_hash(f.schema for f in ordered)
    return revision_id([f.sha256 for f in ordered], sh), sh


def _rows(path: Path) -> int:
    import pyarrow.parquet as pq

    return int(pq.read_metadata(path).num_rows)


def _examlops_version() -> str:
    try:
        from importlib.metadata import version

        return version("examlops")
    except Exception:
        return ""


def publish(
    store: DatasetStore,
    key: str,
    *,
    staged: list[tuple[str, Path]],
    parent: SnapshotManifest | None,
    connector: str,
    connection: str | None,
    spec_hash: str,
    watermark: dict[str, Any],
    pull_id: str,
    incremental: bool,
) -> tuple[SnapshotManifest, bool]:
    """Upload parts, write the manifest and pointers. Returns (manifest, changed).

    ``changed`` is false — and nothing is written — only when the content revision **and** the
    spec hash both equal the parent's. Identical data read under a changed spec is still a new
    snapshot: it records the new ``spec_hash``, otherwise every later pull would see the spec
    mismatch again and run full forever. The two pulls then share one revision (``prune`` keeps
    the shared ``_revisions`` pointer on whichever of them it keeps)."""
    carried: list[ManifestFile] = list(parent.files) if (incremental and parent) else []
    offsets: dict[str, int] = {}
    for f in carried:
        table = PurePosixPath(f.path).parts[0]
        offsets[table] = offsets.get(table, 0) + 1
    new: list[tuple[ManifestFile, Path]] = []
    for rel, local in staged:
        table, name = PurePosixPath(rel).parts[0], PurePosixPath(rel).name
        idx = int(name.removeprefix("part-").removesuffix(".parquet")) + offsets.get(table, 0)
        rel_final = f"{table}/part-{idx:05d}.parquet"
        mf = ManifestFile(
            path=rel_final,
            object=f"{key}/{pull_id}/{rel_final}",
            sha256=file_digest(local),
            bytes=local.stat().st_size,
            rows=_rows(local),
            schema=schema_part(local) or "",
        )
        new.append((mf, local))
    files = sorted(carried + [m for m, _ in new], key=lambda f: PurePosixPath(f.path).parts)
    rev, sh = _revision_of(files)
    if parent is not None and rev == parent.revision and parent.spec_hash == spec_hash:
        return parent, False
    for mf, local in new:
        store.put_file(local, mf.object)
    manifest = SnapshotManifest(
        source=key,
        connector=connector,
        connection=connection,
        spec_hash=spec_hash,
        revision=rev,
        schema_hash=sh,
        files=tuple(files),
        tables=tuple(sorted({PurePosixPath(f.path).parts[0] for f in files})),
        row_count=sum(f.rows for f in files),
        byte_count=sum(f.bytes for f in files),
        watermark=watermark,
        parent_revision=parent.revision if parent else None,
        pull_id=pull_id,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        examlops_version=_examlops_version(),
    )
    store.write_text(f"{key}/{pull_id}/_manifest.json", manifest.to_json())
    store.write_text(f"{key}/_revisions/{rev}", pull_id)
    write_latest(store, key, rev, pull_id)
    return manifest, True


def write_latest(store: DatasetStore, key: str, revision: str, pull_id: str) -> None:
    """Point ``_latest`` at a committed snapshot — the commit itself. Only a holder of the
    source's pull lock may call it (``publish``, or the reaper finishing a crashed commit)."""
    store.write_text(f"{key}/_latest", json.dumps({"revision": revision, "pull_id": pull_id}))


def resolve(store: DatasetStore, key: str, revision: str | None = "latest") -> SnapshotRef:
    """The committed snapshot ``revision`` names: ``latest`` (or empty), or a full revision id.

    Anything else is refused before it can become part of a store key or a cache directory name
    (``../_latest`` would otherwise read another object as a pointer)."""
    if revision not in (None, "", "latest") and not _REVISION_RE.fullmatch(str(revision)):
        raise SpecError(
            "a snapshot revision is 'latest' or a full 64-character lowercase hex revision id"
        )
    try:
        if revision in (None, "", "latest"):
            data = json.loads(store.read_text(f"{key}/_latest"))
            rev, pull_id = data["revision"], data["pull_id"]
        else:
            rev, pull_id = revision, store.read_text(f"{key}/_revisions/{revision}").strip()
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise SnapshotNotFound(f"no committed snapshot for {key}@{revision or 'latest'}") from None
    return SnapshotRef(
        source=key, revision=rev, pull_id=pull_id, manifest_key=f"{key}/{pull_id}/_manifest.json"
    )


def read_manifest(store: DatasetStore, ref: SnapshotRef) -> SnapshotManifest:
    try:
        return SnapshotManifest.from_json(store.read_text(ref.manifest_key))
    except FileNotFoundError:
        raise SnapshotNotFound(f"manifest missing for {ref.source}@{ref.revision}") from None


_MATERIALIZE_MAX_RENAME_ATTEMPTS = 50


def _materialize_marker_valid(dest: Path, revision: str) -> bool:
    try:
        return (dest / ".complete").read_text() == revision
    except OSError:
        return False


def materialize(store: DatasetStore, ref: SnapshotRef, cache_root: Path) -> Path:
    """Download a revision into ``cache_root/<revision>/`` once, checksum-verified.

    Safe under concurrent callers (threads or processes) sharing ``cache_root``: every caller
    downloads into its own uniquely-named tmp dir (pid **and** a random suffix — pid alone
    collides between threads of the same process, which used to let one caller's cleanup delete
    another's in-flight download), verifies every file's checksum there, and writes the
    completion marker *inside* the tmp dir before promoting it — so a directory at ``dest`` is
    either fully absent or fully complete, never partial. Promotion is an ``os.rename`` (atomic on
    a POSIX filesystem); a racing loser whose rename fails re-checks the marker a winner may have
    just written and, if valid, discards its own download and returns the winner's ``dest``. A
    ``dest`` left behind by a crashed earlier attempt (present but without a valid marker) is
    cleared and the rename retried.
    """
    dest = Path(cache_root) / ref.revision
    if _materialize_marker_valid(dest, ref.revision):
        return dest
    manifest = read_manifest(store, ref)
    # The revision id is a content address: the manifest's digests must hash back to it, or the
    # data behind a pinned id was swapped (the per-file checks below would then only prove the
    # files match a *rewritten* manifest). Checked before a single byte is downloaded.
    expected, _sh = _revision_of(manifest.files)
    if expected != ref.revision or manifest.revision != ref.revision:
        raise SnapshotIntegrityError(
            f"{ref.source}@{ref.revision}: the manifest's file digests hash to revision "
            f"{expected}, not the one requested — the snapshot was modified after it was committed"
        )
    tmp = Path(cache_root) / f".{ref.revision}.{os.getpid()}.{uuid4().hex}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for f in manifest.files:
        local = tmp / f.path
        store.get_file(f.object, local)
        if file_digest(local) != f.sha256:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SnapshotIntegrityError(
                f"checksum mismatch for {f.path} in {ref.source}@{ref.revision}"
            )
    (tmp / ".complete").write_text(ref.revision)
    for _attempt in range(_MATERIALIZE_MAX_RENAME_ATTEMPTS):
        try:
            os.rename(tmp, dest)
            return dest
        except OSError:
            if _materialize_marker_valid(dest, ref.revision):
                shutil.rmtree(tmp, ignore_errors=True)
                return dest
            shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    raise SnapshotNotFound(
        f"could not materialize {ref.source}@{ref.revision}: destination busy after "
        f"{_MATERIALIZE_MAX_RENAME_ATTEMPTS} attempts"
    )


def _looks_like_pull_id(pid: str) -> bool:
    """True when ``pid`` starts with the 16-hex-nanosecond prefix ``new_pull_id`` produces."""
    prefix = pid[:16]
    return len(prefix) == 16 and all(c in "0123456789abcdef" for c in prefix.lower())


def _last_activity(store: DatasetStore, key: str, pid: str) -> float:
    """When a pull dir was last active: its newest object's mtime (the pull's start, from its id,
    when the listing carries no modification times).

    A pull uploads its parts only when it commits, so a pull that started long ago (a slow read)
    can be uploading right now; the start time alone would call its fresh parts garbage and delete
    them before the manifest lands. The later of the two is used, so a store clock running behind
    this host's can only make a dir look younger (kept longer), never older."""
    try:
        newest = store.newest_mtime(f"{key}/{pid}")
    except Exception:  # noqa: BLE001 - an unreadable listing falls back to the pull's start time
        newest = None
    started = int(pid[:16], 16) / 1_000_000_000
    return max(newest, started) if newest is not None else started


def prune(
    store: DatasetStore, key: str, *, keep: int, pinned: set[str], dry_run: bool = False
) -> list[str]:
    """Delete old pull dirs. Keeps the newest ``keep`` committed revisions, every pinned revision,
    ``_latest``, and any file a kept manifest references.

    A revision pointer (``_revisions/<rev>``) is only deleted when no *kept* pull's manifest still
    carries that revision. ``publish`` dedups a new pull only against its *immediate* parent, so a
    non-adjacent pull can independently land back on a revision a kept pull still has (A -> B ->
    A): removing the pull that originally wrote the pointer must not take the pointer away from
    the kept pull that shares its revision — instead the pointer is rewritten to name the kept
    pull. Returns removed pull ids.
    """
    pulls = [d for d in store.ls_dirs(key) if not d.startswith("_")]
    manifests: dict[str, SnapshotManifest] = {}
    for pid in pulls:
        if store.exists(f"{key}/{pid}/_manifest.json"):
            manifests[pid] = SnapshotManifest.from_json(
                store.read_text(f"{key}/{pid}/_manifest.json")
            )
    try:
        latest = resolve(store, key).pull_id
    except SnapshotNotFound:
        latest = ""
    newest = sorted(manifests, reverse=True)[: max(keep, 0)]
    kept = {
        pid
        for pid, m in manifests.items()
        if pid in newest or pid == latest or m.revision in pinned
    }
    referenced = {f.object.split("/")[-3] for pid in kept for f in manifests[pid].files}
    kept_revisions: dict[str, str] = {}
    for kp in kept:
        kept_revisions.setdefault(manifests[kp].revision, kp)
    now = time.time()
    removed = []
    for pid in pulls:
        if pid in kept or pid in referenced:
            continue
        # A dir whose name parses as a real pull id (R1: 16-hex-nanosecond prefix) gets an age
        # grace regardless of whether it has landed a manifest yet — it may still be running, or
        # mid-commit (manifest + _revisions written, `_latest` not yet). A dir whose name does not
        # parse (legacy/test ids) can never be a real in-flight pull and is removed outright
        # (never crash on the parse).
        if _looks_like_pull_id(pid) and now - _last_activity(store, key, pid) < _ORPHAN_AGE_S:
            continue  # too young; may still be in flight or mid-commit
        removed.append(pid)
        if not dry_run:
            store.rm(f"{key}/{pid}")
            if pid in manifests:
                rev = manifests[pid].revision
                alt = kept_revisions.get(rev)
                if alt is not None:
                    store.write_text(f"{key}/_revisions/{rev}", alt)
                else:
                    store.rm(f"{key}/_revisions/{rev}")
    return removed
