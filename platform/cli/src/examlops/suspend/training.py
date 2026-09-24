"""``training-checkpoint`` — the suspend backend for distributed training runs (ADR 0109 dec. 4).

ADR 0109's default backend is "reuse the checkpoints the platform already writes". Until now that
meant *agent-session* checkpoints only (``checkpoint-only``). The platform also writes real
training checkpoints: ADR 0032's reference DDP job commits, per step, one shard file per rank plus
a manifest carrying each shard's SHA-256 and the run's ``config_hash``
(:mod:`examlops.distributed.checkpoint_files`). That is exactly the durable state this seam is
supposed to be able to pin, verify and hand back.

So this backend is a *thin* adapter, and deliberately so:

* **It never reimplements integrity.** Every eligibility decision goes through
  ``checkpoint_files.find_latest_valid`` / ``verify_checkpoint_dir``, which parse the manifest,
  re-digest it, compare the ``config_hash`` and re-hash every shard. A corrupt or half-written
  step is invalid there, and the fallback to the previous valid step is that function's, not ours.
* **Snapshot pins, it does not copy.** A pin is a small marker file written next to the manifest
  (``.suspend-pin-<snapshot_id>.json``); :func:`pinned_steps` lets a retention pass see which
  steps a live suspend record still needs. Copying tens of GB of shards to "suspend" them would be
  a second, unverified copy of state that is already durable.
* **Restore points a resumed run at the pinned checkpoint; it does not load tensors.** Loading is
  the training script's job (``train_ddp`` selects and reassembles the newest valid checkpoint at
  start-up, under torch, on the training nodes). Restore re-verifies the pinned step — which reads
  and hashes every shard byte, so ``state_transfer_s`` is a genuine measurement of reading the
  state back — and reports where a relaunch resumes from.

What it therefore **cannot** persist, and says so in ``capability().notes``: GPU memory, the
process image, the collective communicator, the dataloader iterator, or any RNG stream the run
does not re-derive from its seed. ``communicator_rebuild_applicable`` is ``True`` (a resumed
distributed run does rebuild its process group) and ``communicator_rebuild_s`` stays ``None``,
because nothing here has measured it — an invented number is the failure ADR 0109 decision 2
forbids.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from examlops.distributed import checkpoint_files as cf

from .backends import _BackendBase
from .types import (
    STATE_TRAINING_RUN,
    Capability,
    RestoreReport,
    SnapshotHandle,
    SuspendError,
)

#: Marker file written inside a pinned ``step-*`` directory. A dotfile, and not a shard or the
#: manifest, so ``verify_checkpoint_dir`` neither sees it nor is disturbed by it.
PIN_PREFIX = ".suspend-pin-"
PIN_SUFFIX = ".json"


def _run_dir(subject_id: str, options: Mapping[str, Any] | None) -> Path:
    """The run directory: ``options['run_dir']``, else the launcher's default for this run id."""
    raw = (options or {}).get("run_dir")
    if raw:
        return Path(str(raw))
    from examlops.distributed.launch import default_run_dir

    return default_run_dir(subject_id)


def pinned_steps(run_dir: Path | str) -> dict[int, list[str]]:
    """``{step: [snapshot_id, ...]}`` for every step a suspend snapshot still pins.

    A retention or prune pass reads this before deleting old checkpoints; nothing here deletes
    anything.
    """
    out: dict[int, list[str]] = {}
    for st in cf.list_checkpoints(Path(run_dir)):
        if st.directory is None:
            continue
        ids = sorted(
            p.name[len(PIN_PREFIX) : -len(PIN_SUFFIX)]
            for p in st.directory.glob(f"{PIN_PREFIX}*{PIN_SUFFIX}")
        )
        if ids:
            out[st.step] = ids
    return out


def resume_target(handle: SnapshotHandle) -> dict[str, Any]:
    """Where a relaunched run picks this snapshot up — the "pointing at it" part of a restore."""
    ptr = handle.pointer or {}
    return {
        "run_dir": ptr.get("run_dir"),
        "step": ptr.get("step"),
        "world_size": ptr.get("world_size"),
        "config_hash": ptr.get("config_hash"),
    }


class TrainingCheckpointBackend(_BackendBase):
    """Suspend a distributed training run by pinning its newest checkpoint that verifies."""

    name = "training-checkpoint"

    def capability(self) -> Capability:
        return Capability(
            backend=self.name,
            granularity="application",
            state_kinds=(STATE_TRAINING_RUN,),
            tiers=("persistent_storage",),
            peer_replication=False,
            gpu_state=False,
            # A resumed distributed run *does* rebuild its process group, so the cost applies …
            communicator_rebuild_applicable=True,
            # … and nobody here has measured it. None, not a number copied out of a paper.
            communicator_rebuild_s=None,
            restore_fixed_s=None,
            restore_throughput_mb_s=None,
            basis="unknown",
            notes=(
                "Framework-level (ADR 0032): pins the newest sharded checkpoint whose manifest "
                "digest, config hash and per-shard SHA-256 all verify. Persists what the training "
                "script wrote - model parameters and optimizer state, sharded across ranks. Does "
                "NOT persist GPU memory, the process image, the collective communicator, the "
                "dataloader iterator, or RNG state the run does not re-derive from its seed. "
                "Restore verifies and reads the pinned checkpoint; the training job reassembles "
                "the tensors and rebuilds its process group when it is relaunched, and that "
                "rebuild has not been measured here."
            ),
        )

    def snapshot(
        self, subject_kind: str, subject_id: str, *, options: dict[str, Any] | None = None
    ) -> SnapshotHandle:
        self._check(self.capability(), subject_kind, options)
        opts = dict(options or {})
        run_dir = _run_dir(subject_id, opts)
        cfg_hash = opts.get("config_hash")
        latest, skipped = cf.find_latest_valid(run_dir, cfg_hash)
        if latest is None or latest.directory is None:
            why = "; ".join(f"step {s.step}: {s.reason}" for s in skipped) or "none on disk"
            raise SuspendError(
                f"no valid checkpoint for run {subject_id!r} under {run_dir} "
                f"(rejected: {why}); nothing to suspend"
            )
        manifest = latest.manifest
        snapshot_id = uuid.uuid4().hex
        pin = f"{PIN_PREFIX}{snapshot_id}{PIN_SUFFIX}"
        cf.atomic_write_bytes(
            latest.directory / pin,
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "run_id": subject_id,
                    "step": latest.step,
                    "manifest_sha256": manifest["manifest_sha256"],
                    "pinned_at": time.time(),
                },
                sort_keys=True,
            ).encode(),
        )
        return SnapshotHandle(
            snapshot_id=snapshot_id,
            backend=self.name,
            subject_kind=subject_kind,
            subject_id=subject_id,
            pointer={
                "run_dir": str(run_dir),
                "step": latest.step,
                "epoch": manifest.get("epoch"),
                "world_size": manifest.get("world_size"),
                "config_hash": manifest.get("config_hash"),
                "manifest_sha256": manifest["manifest_sha256"],
                "pin_file": pin,
                # Newer steps that did not verify, so the operator can see what was skipped.
                "skipped": [{"step": s.step, "reason": s.reason} for s in skipped],
            },
            state_bytes=sum(int(s.get("bytes") or 0) for s in manifest.get("shards") or []),
            created_at=time.time(),
        )

    def restore(self, handle: SnapshotHandle) -> RestoreReport:
        ptr = handle.pointer or {}
        raw_dir, raw_step = ptr.get("run_dir"), ptr.get("step")
        if not raw_dir or raw_step is None:
            return RestoreReport(False, None, None, "snapshot pointer has no run_dir/step")
        run_dir, step = Path(str(raw_dir)), int(raw_step)
        cfg_hash = ptr.get("config_hash")
        t0 = time.perf_counter()
        status = cf.verify_checkpoint_dir(cf.step_dir(run_dir, step), cfg_hash)
        elapsed = time.perf_counter() - t0  # the shards were read and hashed to get here
        if not status.valid:
            return RestoreReport(
                False,
                None,
                None,
                f"pinned checkpoint step {step} no longer verifies: {status.reason}",
            )
        pinned_digest = ptr.get("manifest_sha256")
        if pinned_digest and status.manifest.get("manifest_sha256") != pinned_digest:
            return RestoreReport(
                False, None, None, f"step {step} was rewritten since the snapshot was taken"
            )
        newest, _ = cf.find_latest_valid(run_dir, cfg_hash)
        drift = ""
        if newest is not None and newest.step != step:
            drift = (
                f" NOTE: a newer valid checkpoint (step {newest.step}) now exists and the training "
                "script selects the newest, so a relaunch would resume there, not at the pinned "
                "step."
            )
        return RestoreReport(
            True,
            elapsed,
            # Applicable (a relaunched run rebuilds its process group) but not measured here.
            None,
            (
                f"verified and read {status.manifest.get('world_size')} shard(s) of step {step} "
                f"under {run_dir}; a run relaunched against this run directory resumes from it "
                "(the job loads the tensors and rebuilds its communicator)." + drift
            ),
        )

    def discard(self, handle: SnapshotHandle) -> None:
        """Release the pin. The checkpoint belongs to the training run and is never deleted."""
        ptr = handle.pointer or {}
        pin, raw_dir, raw_step = ptr.get("pin_file"), ptr.get("run_dir"), ptr.get("step")
        if not (pin and raw_dir and raw_step is not None):
            return None
        try:
            (cf.step_dir(Path(str(raw_dir)), int(raw_step)) / str(pin)).unlink(missing_ok=True)
        except OSError:  # a read-only or vanished run directory is not a failure to discard
            pass
        return None
