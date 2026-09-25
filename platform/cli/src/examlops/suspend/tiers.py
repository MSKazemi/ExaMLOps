"""Tiered suspend for training runs (ADR 0109 decision 8).

Decision 8 asks for *two* tiers because one cannot serve both failure classes: a node-local copy
recovers a localized fault fast and dies with the node; the shared persistent checkpoint survives
a cluster-wide outage and is slower to read back. :class:`TrainingCheckpointBackend` already pins
the persistent tier. This module adds the local one.

``LocalTier`` keeps a **verified replica** of pinned ADR 0032 checkpoints on node-local storage
(``EXAMLOPS_SUSPEND_LOCAL_DIR``). Its layout is itself a valid run directory, so every integrity
decision is ``checkpoint_files``' and nothing here re-implements hashing::

    <root>/<run-key>/objects/<sha256>               content-addressed shard bytes
    <root>/<run-key>/ckpt/step-NNNNNNNN/<shard>     hard link (or copy) of the object
    <root>/<run-key>/ckpt/step-NNNNNNNN/manifest.json

Staging is **differential at shard granularity**: a shard whose SHA-256 is already held as an
object is linked, not copied, so only changed shards cost bytes and I/O. That is weaker than
TierCheck's in-memory tensor diffs (a DDP rank's shard changes every step, so for full fine-tuning
little is reused) and the result says exactly how many bytes were copied versus reused.

The tier label is measured, not declared: a root on ``tmpfs``/``ramfs`` is ``local_memory``,
anything else ``local_storage``. There is no ``peer_memory`` tier - replicating to a peer needs a
transport between nodes that this platform does not have, and it is not faked.

Bounded: staging refuses when the tier would exceed ``EXAMLOPS_SUSPEND_LOCAL_MAX_BYTES``
(default 4 GiB) and the snapshot then degrades to persistent-only, saying so in its pointer.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from examlops.distributed import checkpoint_files as cf

from .training import PIN_PREFIX, PIN_SUFFIX, TrainingCheckpointBackend
from .types import Capability, RestoreReport, SnapshotHandle, SuspendError

LOCAL_DIR_ENV = "EXAMLOPS_SUSPEND_LOCAL_DIR"
LOCAL_MAX_ENV = "EXAMLOPS_SUSPEND_LOCAL_MAX_BYTES"
DEFAULT_LOCAL_MAX_BYTES = 4 * 1024**3
_MEMORY_FS = frozenset({"tmpfs", "ramfs"})


class LocalTierError(SuspendError):
    """The local tier could not stage or serve a checkpoint. Callers degrade to persistent."""


def _mount_fstype(path: Path, mounts_file: str = "/proc/mounts") -> str | None:
    """Filesystem type of the mount holding ``path`` (longest mount-point prefix), or None."""
    try:
        lines = Path(mounts_file).read_text().splitlines()
    except OSError:
        return None
    target = str(path.resolve())
    best, fstype = "", None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, fstype = mnt, parts[2]
    return fstype


def _max_bytes() -> int:
    raw = os.getenv(LOCAL_MAX_ENV, "").strip()
    if not raw:
        return DEFAULT_LOCAL_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise LocalTierError(f"{LOCAL_MAX_ENV} must be an integer byte count, got {raw!r}") from exc
    if value <= 0:
        raise LocalTierError(f"{LOCAL_MAX_ENV} must be positive, got {value}")
    return value


def run_key(subject_id: str, persistent_run_dir: Path | str) -> str:
    """A filesystem-safe, collision-resistant key for one run's replica directory."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in subject_id)[:48] or "run"
    digest = hashlib.sha256(str(Path(persistent_run_dir).resolve()).encode()).hexdigest()[:16]
    return f"{safe}-{digest}"


class LocalTier:
    """A verified, content-addressed, size-capped replica of pinned checkpoints on one node."""

    def __init__(self, root: Path | str, *, max_bytes: int | None = None) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes if max_bytes is not None else _max_bytes()

    @classmethod
    def from_env(cls) -> LocalTier | None:
        raw = os.getenv(LOCAL_DIR_ENV, "").strip()
        return cls(raw) if raw else None

    # ── introspection ───────────────────────────────────────────────────────────────────────

    def tier(self) -> str:
        """``local_memory`` on tmpfs/ramfs, otherwise ``local_storage`` (measured from mounts)."""
        probe = self.root if self.root.exists() else self.root.parent
        return "local_memory" if _mount_fstype(probe) in _MEMORY_FS else "local_storage"

    def run_dir(self, key: str) -> Path:
        return self.root / key

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise stage / release / GC across processes on this node (``flock`` on the root).

        Without it a discard's GC, which deletes every object no manifest references yet, can
        remove another process's in-flight copy or a just-copied object whose manifest is not
        written, and two stagings can each pass the cap check and together exceed it.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def used_bytes(self) -> int:
        total = 0
        if not self.root.is_dir():
            return 0
        for obj_dir in self.root.glob("*/objects"):
            for f in obj_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    total += f.stat().st_size
        return total

    # ── staging ─────────────────────────────────────────────────────────────────────────────

    def stage(self, key: str, source_step_dir: Path, snapshot_id: str) -> dict[str, Any]:
        """Replicate one verified persistent step into the tier; returns copy/reuse accounting.

        The source is re-verified first, every copied object is re-hashed after the copy, and
        the replica step is verified as a checkpoint directory before it is reported staged, so
        a torn read can never become the fast-path copy.
        """
        src = cf.verify_checkpoint_dir(source_step_dir)
        if not src.valid:
            raise LocalTierError(f"source step does not verify: {src.reason}")
        manifest = src.manifest
        shards = list(manifest.get("shards") or [])
        run_dir = self.run_dir(key)
        dest = cf.step_dir(run_dir, int(manifest["step"]))
        with self._locked():
            return self._stage_or_clean(
                run_dir, dest, source_step_dir, manifest, shards, snapshot_id
            )

    def _stage_or_clean(
        self,
        run_dir: Path,
        dest: Path,
        source_step_dir: Path,
        manifest: dict[str, Any],
        shards: list[dict[str, Any]],
        snapshot_id: str,
    ) -> dict[str, Any]:
        key = run_dir.name
        existed = dest.is_dir()
        try:
            return self._stage(run_dir, dest, source_step_dir, manifest, shards, snapshot_id)
        except BaseException:
            # A half-staged step must not survive: its shard links pin bytes that the cap
            # (which counts objects) cannot see, and nothing would ever release it. A step that
            # already held another snapshot's verified replica is left alone - only this
            # snapshot's own pin is removed.
            (dest / f"{PIN_PREFIX}{snapshot_id}{PIN_SUFFIX}").unlink(missing_ok=True)
            if not existed or not any(dest.glob(f"{PIN_PREFIX}*{PIN_SUFFIX}")):
                shutil.rmtree(dest, ignore_errors=True)
            try:
                self._gc(key)
            except OSError:
                pass
            raise

    def _stage(
        self,
        run_dir: Path,
        dest: Path,
        source_step_dir: Path,
        manifest: dict[str, Any],
        shards: list[dict[str, Any]],
        snapshot_id: str,
    ) -> dict[str, Any]:
        objects = run_dir / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        dest.mkdir(parents=True, exist_ok=True)

        missing: dict[str, int] = {}
        for s in shards:
            sha, obj = str(s["sha256"]), objects / str(s["sha256"])
            if not obj.is_file() or obj.stat().st_size != s.get("bytes"):
                missing[sha] = int(s.get("bytes") or 0)
        need = sum(missing.values())
        used = self.used_bytes()
        if used + need > self.max_bytes:
            raise LocalTierError(
                f"local tier cap exceeded: {used} B used + {need} B needed > {self.max_bytes} B "
                f"({LOCAL_MAX_ENV})"
            )

        copied = reused = 0
        for s in shards:
            sha, size = str(s["sha256"]), int(s.get("bytes") or 0)
            obj = objects / sha
            if sha in missing:
                tmp = objects / f".{sha}.tmp.{os.getpid()}"
                shutil.copyfile(source_step_dir / str(s["file"]), tmp)
                if cf.sha256_file(tmp) != sha:
                    tmp.unlink(missing_ok=True)
                    raise LocalTierError(f"shard {s.get('rank')} changed while being copied")
                os.replace(tmp, obj)
                missing.pop(sha)  # a second shard with identical bytes reuses this object
                copied += size
            else:
                reused += size
            link = dest / str(s["file"])
            link.unlink(missing_ok=True)
            try:
                os.link(obj, link)
            except OSError:  # a filesystem without hard links still gets a verified copy
                shutil.copyfile(obj, link)
        cf.atomic_write_bytes(dest / cf.MANIFEST, (source_step_dir / cf.MANIFEST).read_bytes())
        cf.atomic_write_bytes(
            dest / f"{PIN_PREFIX}{snapshot_id}{PIN_SUFFIX}",
            json.dumps({"snapshot_id": snapshot_id, "pinned_at": time.time()}).encode(),
        )
        check = cf.verify_checkpoint_dir(dest)
        if not check.valid:
            raise LocalTierError(f"replica does not verify after staging: {check.reason}")
        return {
            "tier": self.tier(),
            "run_dir": str(run_dir),
            "step": int(manifest["step"]),
            "copied_bytes": copied,
            "reused_bytes": reused,
            "manifest_sha256": manifest["manifest_sha256"],
        }

    # ── release ─────────────────────────────────────────────────────────────────────────────

    def release(self, key: str, step: int, snapshot_id: str) -> None:
        """Drop one pin; delete the replica step when no pin remains, then GC orphan objects."""
        run_dir = self.run_dir(key)
        dest = cf.step_dir(run_dir, step)
        with self._locked():
            (dest / f"{PIN_PREFIX}{snapshot_id}{PIN_SUFFIX}").unlink(missing_ok=True)
            if dest.is_dir() and not any(dest.glob(f"{PIN_PREFIX}*{PIN_SUFFIX}")):
                shutil.rmtree(dest, ignore_errors=True)
            self._gc(key)

    def gc(self, key: str) -> int:
        """Delete objects no remaining replica manifest references. Returns bytes freed."""
        with self._locked():
            return self._gc(key)

    def _gc(self, key: str) -> int:
        run_dir = self.run_dir(key)
        objects = run_dir / "objects"
        if not objects.is_dir():
            return 0
        # Read the manifests, do not verify them: verification re-hashes every replica byte on
        # every discard, and a step whose shards are damaged must still keep its references
        # (only a step with no readable manifest at all references nothing).
        referenced: set[str] = set()
        ckpt_root = run_dir / cf.CKPT_DIR
        step_dirs = list(ckpt_root.iterdir()) if ckpt_root.is_dir() else []
        for d in step_dirs:
            try:
                data = json.loads((d / cf.MANIFEST).read_bytes())
            except (OSError, ValueError):
                continue
            for s in (data.get("shards") if isinstance(data, dict) else None) or []:
                if isinstance(s, dict) and s.get("sha256"):
                    referenced.add(str(s["sha256"]))
        freed = 0
        for f in objects.iterdir():
            if f.is_file() and f.name not in referenced:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
        return freed


class TieredTrainingCheckpointBackend(TrainingCheckpointBackend):
    """``training-checkpoint`` plus a node-local replica tier (ADR 0109 decision 8).

    ``snapshot`` pins the persistent step exactly as the parent does, then stages a verified
    replica into the local tier. A staging failure (no tier configured, cap exceeded, a shard
    changing mid-copy) never fails the snapshot: the persistent pin stands and the pointer says
    why the local tier was skipped. ``restore`` serves from the local replica when it still
    verifies against the pinned manifest digest - the localized-fault fast path - and otherwise
    falls back to the persistent tier, reporting which tier answered.
    """

    name = "tiered-training-checkpoint"

    def __init__(self, local: LocalTier | None = None) -> None:
        self._local = local

    def _tier(self) -> tuple[LocalTier | None, str | None]:
        """The local tier, or ``(None, why)``. A misconfigured tier degrades, it never raises:
        ``snapshot`` has already pinned the persistent step by the time it asks, and the service
        reads ``capability()`` after the snapshot to record it."""
        if self._local is not None:
            return self._local, None
        try:
            local = LocalTier.from_env()
        except LocalTierError as exc:
            return None, f"local tier misconfigured: {exc}"
        return local, (None if local is not None else f"{LOCAL_DIR_ENV} not set")

    def capability(self) -> Capability:
        base = super().capability()
        local, why = self._tier()
        if local is None:
            tiers: tuple[str, ...] = ("persistent_storage",)
            extra = (
                f" No local tier ({why}): set {LOCAL_DIR_ENV} (and a positive integer "
                f"{LOCAL_MAX_ENV}) to enable the node-local replica."
            )
        else:
            tiers = (local.tier(), "persistent_storage")
            extra = (
                f" Local tier: a verified, shard-differential replica under {local.root} "
                f"({tiers[0]}, cap {local.max_bytes} B) serves localized-fault restores; it is "
                "lost with the node, and the persistent pin covers cluster-wide outages. No "
                "peer-memory tier exists."
            )
        return Capability(
            backend=self.name,
            granularity=base.granularity,
            state_kinds=base.state_kinds,
            tiers=tiers,
            peer_replication=False,
            gpu_state=False,
            communicator_rebuild_applicable=True,
            communicator_rebuild_s=None,
            restore_fixed_s=None,
            restore_throughput_mb_s=None,
            basis="unknown",
            notes=(base.notes + extra).strip(),
        )

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        handle = super().snapshot(subject_kind, subject_id, options=options)
        ptr = dict(handle.pointer)
        local, why = self._tier()
        if local is None:
            ptr["local_tier"] = {"staged": False, "reason": why}
        else:
            key = run_key(subject_id, ptr["run_dir"])
            try:
                info = local.stage(
                    key, cf.step_dir(Path(ptr["run_dir"]), int(ptr["step"])), handle.snapshot_id
                )
                ptr["local_tier"] = {"staged": True, "key": key, "root": str(local.root), **info}
            except (LocalTierError, OSError) as exc:
                ptr["local_tier"] = {"staged": False, "key": key, "reason": str(exc)}
        return SnapshotHandle(
            snapshot_id=handle.snapshot_id,
            backend=self.name,
            subject_kind=handle.subject_kind,
            subject_id=handle.subject_id,
            pointer=ptr,
            state_bytes=handle.state_bytes,
            created_at=handle.created_at,
        )

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        ptr = handle.pointer or {}
        lt = ptr.get("local_tier") or {}
        fallback_why = lt.get("reason") or "local replica not staged"
        if lt.get("staged") and lt.get("run_dir") and ptr.get("step") is not None:
            t0 = time.perf_counter()
            status = cf.verify_checkpoint_dir(
                cf.step_dir(Path(str(lt["run_dir"])), int(ptr["step"])), ptr.get("config_hash")
            )
            elapsed = time.perf_counter() - t0
            if status.valid and status.manifest.get("manifest_sha256") == ptr.get(
                "manifest_sha256"
            ):
                return RestoreReport(
                    True,
                    elapsed,
                    None,
                    f"served from the {lt.get('tier', 'local')} tier: verified and read the "
                    f"replica of step {ptr['step']} under {lt['run_dir']}; a run relaunched "
                    "against that directory resumes from it without touching the persistent "
                    "tier (the job rebuilds its communicator; not measured here).",
                )
            fallback_why = status.reason or "replica manifest differs from the pinned step"
        report = super().restore(handle)
        return RestoreReport(
            report.restored,
            report.state_transfer_s,
            report.communicator_rebuild_s,
            f"persistent tier (local tier unavailable: {fallback_why}); {report.detail}",
        )

    def discard(self, handle: SnapshotHandle) -> None:
        super().discard(handle)
        ptr = handle.pointer or {}
        lt = ptr.get("local_tier") or {}
        if not (lt.get("staged") and lt.get("key") and lt.get("root")):
            return None
        try:
            LocalTier(str(lt["root"]), max_bytes=1).release(
                str(lt["key"]), int(ptr["step"]), handle.snapshot_id
            )
        except OSError:  # a node that no longer has the replica has nothing to release
            pass
        return None
