"""Durable checkpoint storage for distributed training (ADR 0032 decision 2).

A run directory is scratch: the node that wrote it may be gone when the resubmitted job starts, and
a site's scratch filesystem is purged. This module keeps a **durable copy** of every checkpoint that
verifies, keyed by run, and restores the newest valid one into an empty run directory before a
(re)submission — so "resume" survives losing the node, not only losing the process.

Store, one of (``EXAMLOPS_DIST_CHECKPOINT_STORE`` or ``--checkpoint-store``):

* a local directory — the NFS / Lustre / GPFS mount shared by the login and compute nodes;
* ``file://…`` — the same, spelled as a URL;
* ``s3://bucket/prefix`` — MinIO or any S3-compatible store, through pyarrow's native filesystem
  (``examlops.dataplane.s3``; no s3fs). Endpoint ``EXAMLOPS_DIST_CHECKPOINT_S3_ENDPOINT`` else
  ``MLFLOW_S3_ENDPOINT_URL``; credentials from ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` or
  pyarrow's default chain (never anonymous);
* ``gs://`` / ``gcs://`` / ``hdfs://`` (and ``memory://`` in tests) through fsspec when it is
  installed (``examlops[dataplane-files]``). Other fsspec protocols (http, ftp, sftp, …) are
  refused: see :data:`STORE_SCHEMES`.

A store that is configured but unusable is an error (:class:`DurableStoreUnavailable`), never a
silent fallback to scratch: asking for durability and not getting it is exactly the failure this
exists to prevent.

Layout mirrors the run directory, so the same verifier reads both::

    <store>/<run_id>/ckpt/step-00000004/shard-0-of-2.pt
    <store>/<run_id>/ckpt/step-00000004/manifest.json      uploaded LAST — its presence commits

Integrity is re-established on every hop: a checkpoint is mirrored only if it verifies locally, the
upload is size-checked, and a restored copy is re-verified (SHA-256 of every shard against the
manifest) before it is allowed to exist in the run directory. A remote checkpoint that fails is
skipped and the next older one tried — a corrupt copy is never loaded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from examlops.data.audit import audit_best_effort
from examlops.distributed import checkpoint_files as cf

log = logging.getLogger(__name__)

STORE_ENV = "EXAMLOPS_DIST_CHECKPOINT_STORE"
S3_ENDPOINT_ENV = "EXAMLOPS_DIST_CHECKPOINT_S3_ENDPOINT"
AUDIT_SOURCE = "exa-distributed"
#: Object-store schemes a checkpoint store may use (besides a plain directory / ``file://``).
#: ``memory`` is fsspec's in-process store, used by the tests.
STORE_SCHEMES = frozenset({"s3", "gs", "gcs", "hdfs", "memory"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STEP_RE = re.compile(r"^step-(\d{8})$")
_CHUNK = 1 << 20


class DurableStoreUnavailable(RuntimeError):
    """A durable checkpoint store was configured but cannot be used."""


def _safe_run_id(run_id: str) -> str:
    """A run id becomes a store key: refuse anything that could climb out of the store root."""
    if not _RUN_ID_RE.match(run_id or ""):
        raise ValueError(
            f"run id {run_id!r} is not a safe store key (letters, digits, '.', '_', '-'; "
            "must start with a letter or digit; at most 128 characters)"
        )
    return run_id


class CheckpointStore:
    """Minimal file interface over a local directory or an fsspec filesystem."""

    def __init__(self, url: str, root: str, fs: Any | None = None) -> None:
        self.url = url.rstrip("/")
        self.root = root.rstrip("/") or "/"
        self._fs = fs  # None ⇒ the local filesystem, used without fsspec

    # ── paths ────────────────────────────────────────────────────────────────
    def _join(self, *parts: str) -> str:
        return "/".join([self.root, *parts])

    def step_key(self, run_id: str, step: int) -> str:
        return self._join(_safe_run_id(run_id), cf.CKPT_DIR, f"step-{step:08d}")

    def step_uri(self, run_id: str, step: int) -> str:
        return f"{self.url}/{_safe_run_id(run_id)}/{cf.CKPT_DIR}/step-{step:08d}"

    # ── operations ───────────────────────────────────────────────────────────
    def put_file(self, local: Path, key: str) -> int:
        """Copy ``local`` to ``key`` (write-to-temp then rename where the filesystem allows)."""
        if self._fs is None:
            dest = Path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}")
            shutil.copyfile(local, tmp)
            with open(tmp, "rb+") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            return dest.stat().st_size
        parent = key.rsplit("/", 1)[0]
        try:
            self._fs.makedirs(parent, exist_ok=True)
        except Exception:  # noqa: BLE001 - object stores have no directories
            pass
        with open(local, "rb") as src, self._fs.open(key, "wb") as dst:
            while block := src.read(_CHUNK):
                dst.write(block)
        return int(self._fs.size(key))

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        if self._fs is None:
            shutil.copyfile(key, local)
            return
        with self._fs.open(key, "rb") as src, open(local, "wb") as dst:
            while block := src.read(_CHUNK):
                dst.write(block)

    def read_text(self, key: str) -> str | None:
        try:
            if self._fs is None:
                return Path(key).read_text()
            with self._fs.open(key, "rb") as fh:
                return fh.read().decode()
        except (FileNotFoundError, OSError):
            return None

    def list_steps(self, run_id: str) -> list[int]:
        """Committed-or-not step numbers under the run, newest first.

        A run with nothing stored yet is ``[]``. Any other error (store unreachable, access
        denied) is raised: reading "the store is down" as "the store is empty" would silently
        restart a preempted job from step 0 on a node that has no local checkpoint.
        """
        base = self._join(_safe_run_id(run_id), cf.CKPT_DIR)
        try:
            if self._fs is None:
                names = [p.name for p in Path(base).iterdir() if p.is_dir()]
            else:
                names = [
                    str(n).rstrip("/").rsplit("/", 1)[-1] for n in self._fs.ls(base, detail=False)
                ]
        except FileNotFoundError:
            return []
        steps = [int(m.group(1)) for n in names if (m := _STEP_RE.match(n))]
        return sorted(set(steps), reverse=True)


def open_store(url: str) -> CheckpointStore:
    """Build the store named by ``url``. Raises :class:`DurableStoreUnavailable` if unusable."""
    url = url.strip()
    if not url:
        raise DurableStoreUnavailable("empty checkpoint store URL")
    if "://" not in url or url.startswith("file://"):
        root = url[len("file://") :] if url.startswith("file://") else url
        path = Path(root).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DurableStoreUnavailable(
                f"checkpoint store {path} is not writable: {exc}"
            ) from exc
        return CheckpointStore(str(path), str(path))
    scheme = url.split("://", 1)[0].lower()
    if scheme not in STORE_SCHEMES:
        # fsspec would open almost anything (http, ftp, sftp, smb, github, …); a checkpoint store
        # reached through a command the dashboard can run must not become a way to make this host
        # read from or write to an arbitrary network endpoint.
        raise DurableStoreUnavailable(
            f"checkpoint store scheme {scheme!r} is not supported; use a directory, file://, or "
            f"one of {', '.join(s + '://' for s in sorted(STORE_SCHEMES))}"
        )
    opts: dict[str, Any] = {}
    if url.startswith("s3://"):
        endpoint = os.getenv(S3_ENDPOINT_ENV) or os.getenv("MLFLOW_S3_ENDPOINT_URL")
        opts = {
            "key": os.getenv("AWS_ACCESS_KEY_ID"),
            "secret": os.getenv("AWS_SECRET_ACCESS_KEY"),
            "anon": False,
        }
        if endpoint:
            opts["client_kwargs"] = {"endpoint_url": endpoint}
    try:
        from examlops.dataplane.s3 import url_to_fs

        fs, root = url_to_fs(url, **opts)
    except ImportError as exc:
        raise DurableStoreUnavailable(
            f"checkpoint store {url.split('://', 1)[0]}:// needs fsspec/pyarrow "
            f"(pip install 'examlops[dataplane-files]'): {exc}"
        ) from exc
    except (ValueError, OSError) as exc:
        raise DurableStoreUnavailable(f"checkpoint store is not usable: {exc}") from exc
    return CheckpointStore(url, str(root), fs)


def resolve_store(explicit: str | None = None) -> CheckpointStore | None:
    """``explicit`` else ``$EXAMLOPS_DIST_CHECKPOINT_STORE``; ``None`` when neither is set."""
    url = (explicit or os.getenv(STORE_ENV) or "").strip()
    return open_store(url) if url else None


def uri_for(
    store: CheckpointStore | None, run_id: str, *, only: set[int] | None = None
) -> Callable[[int], str | None] | None:
    """``step -> durable URI`` for :func:`launch._register_checkpoints`, or ``None`` (no store).

    With ``only``, steps outside it map to ``None`` — a checkpoint whose upload failed must not be
    recorded under a durable URI it does not have.
    """
    if store is None:
        return None
    return lambda step: store.step_uri(run_id, step) if only is None or step in only else None


@dataclass
class MirrorResult:
    mirrored: list[int]
    already: list[int]
    failed: list[dict[str, Any]]


def _remote_manifest(store: CheckpointStore, run_id: str, step: int) -> dict[str, Any] | None:
    text = store.read_text(f"{store.step_key(run_id, step)}/{cf.MANIFEST}")
    if text is None:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def mirror_run(
    store: CheckpointStore,
    run_id: str,
    run_dir: Path,
    cfg_hash: str | None = None,
    *,
    actor: str | None = None,
) -> MirrorResult:
    """Copy every locally-valid checkpoint not yet committed in the store. Idempotent.

    Shards go first and the manifest last, so a reader of the store sees a step only once all of
    its shards are there. A failure is recorded (and audited) per step and never raised: the next
    attempt mirrors it again, and the training outcome is not decided by the upload.
    """
    res = MirrorResult([], [], [])
    for st in reversed(cf.list_checkpoints(run_dir, cfg_hash)):  # oldest first
        if not st.valid or st.directory is None:
            continue
        remote = _remote_manifest(store, run_id, st.step)
        if remote and remote.get("manifest_sha256") == st.manifest.get("manifest_sha256"):
            res.already.append(st.step)
            continue
        key = store.step_key(run_id, st.step)
        try:
            for s in st.manifest["shards"]:
                size = store.put_file(st.directory / s["file"], f"{key}/{s['file']}")
                if size != s["bytes"]:
                    raise OSError(f"shard {s['rank']}: stored {size} bytes, expected {s['bytes']}")
            store.put_file(st.directory / cf.MANIFEST, f"{key}/{cf.MANIFEST}")
        except Exception as exc:  # noqa: BLE001 - a failed upload is data, see docstring
            res.failed.append({"step": st.step, "error": str(exc)})
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "checkpoint_mirror_failed",
                run_id,
                {"step": st.step, "store": store.url, "error": str(exc)[:500]},
            )
            continue
        res.mirrored.append(st.step)
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "checkpoint_mirrored",
            run_id,
            {"step": st.step, "uri": store.step_uri(run_id, st.step)},
        )
    return res


def restore_latest(
    store: CheckpointStore,
    run_id: str,
    run_dir: Path,
    cfg_hash: str | None = None,
    *,
    actor: str | None = None,
) -> int | None:
    """Bring the newest valid durable checkpoint into ``run_dir`` if it is newer than local's.

    Returns the restored step, or ``None`` when nothing needed (or could be) restored. Every remote
    candidate is downloaded into a temporary directory and verified there; only a copy that
    verifies is renamed into place.
    """
    run_dir = Path(run_dir)
    local, _skipped = cf.find_latest_valid(run_dir, cfg_hash)
    local_step = local.step if local else -1
    try:
        steps = store.list_steps(run_id)
    except Exception as exc:  # noqa: BLE001 - recorded, see below
        # The attempt still runs (from the local checkpoint, if any) — but a store that could not
        # be read is on the record, not indistinguishable from an empty one.
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "checkpoint_store_unreadable",
            run_id,
            {"store": store.url, "error": str(exc)[:500]},
        )
        log.warning("durable store %s could not be listed: %s", store.url, exc)
        return None
    for step in steps:
        if step <= local_step:
            return None
        manifest = _remote_manifest(store, run_id, step)
        if manifest is None:
            continue  # uncommitted upload
        final = cf.step_dir(run_dir, step)
        tmp = final.with_name(final.name + ".restore-tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        key = store.step_key(run_id, step)
        try:
            for s in manifest.get("shards") or []:
                name = str(s.get("file", ""))
                if not name or "/" in name or name.startswith("."):
                    raise OSError(f"unsafe shard name {name!r}")
                store.get_file(f"{key}/{name}", tmp / name)
            store.get_file(f"{key}/{cf.MANIFEST}", tmp / cf.MANIFEST)
        except Exception as exc:  # noqa: BLE001 - try the next older checkpoint
            shutil.rmtree(tmp, ignore_errors=True)
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "checkpoint_restore_failed",
                run_id,
                {"step": step, "error": str(exc)[:500]},
            )
            continue
        # Verify under the final name (the verifier reads the step from the directory name).
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
        status = cf.verify_checkpoint_dir(final, cfg_hash)
        if not status.valid:
            shutil.rmtree(final, ignore_errors=True)
            audit_best_effort(
                AUDIT_SOURCE,
                actor,
                "checkpoint_restore_rejected",
                run_id,
                {"step": step, "reason": status.reason},
            )
            continue
        audit_best_effort(
            AUDIT_SOURCE,
            actor,
            "checkpoint_restored",
            run_id,
            {"step": step, "uri": store.step_uri(run_id, step)},
        )
        return step
    return None


__all__ = [
    "STORE_ENV",
    "CheckpointStore",
    "DurableStoreUnavailable",
    "MirrorResult",
    "mirror_run",
    "open_store",
    "resolve_store",
    "restore_latest",
    "uri_for",
]
