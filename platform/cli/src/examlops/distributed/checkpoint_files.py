"""On-disk sharded checkpoints for distributed training (ADR 0032 decision 2).

Pure standard library — no torch — so the launcher, the tests and ``exa`` can verify a checkpoint
without the training framework installed. The training script (``train_ddp``) does the tensor I/O
and calls these helpers for everything that is about *integrity*.

Layout, under one run directory::

    ckpt/step-00000004/shard-0-of-2.pt     each rank writes its own shard
    ckpt/step-00000004/shard-1-of-2.pt
    ckpt/step-00000004/manifest.json       rank 0 writes it last; its presence commits the step

Every file is written under a temporary name and ``os.replace``d into place, so a reader never sees
a half-written shard or manifest. A checkpoint is **valid** only if the manifest parses, its own
digest matches, its ``config_hash`` equals the run's, and every named shard exists with the recorded
size and SHA-256. Anything else makes that step invalid, and :func:`find_latest_valid` falls back to
the previous one — a corrupt checkpoint is refused, never loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Exit codes of the reference training script. ``torchrun`` collapses a worker failure to its own
#: exit status 1, so the launcher classifies an attempt from these *and* the FATAL marker below.
EXIT_OK = 0
EXIT_FATAL = 70  # deterministic failure: resubmitting would fail again (bad config, NaN loss)
EXIT_RECOVERABLE = 75  # transient: a peer was killed or preempted, the collective broke

FATAL_MARKER = "FATAL.json"
METRICS_MARKER = "EXAMLOPS_DIST_METRICS="
CKPT_DIR = "ckpt"
MANIFEST = "manifest.json"
_STEP_RE = re.compile(r"^step-(\d{8})$")


def step_dir(run_dir: Path, step: int) -> Path:
    return Path(run_dir) / CKPT_DIR / f"step-{step:08d}"


def shard_name(rank: int, world_size: int) -> str:
    return f"shard-{rank}-of-{world_size}.pt"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def config_hash(config: dict[str, Any]) -> str:
    """Digest of the settings a checkpoint is only valid under (model shape, lr, seed, batch).

    Deliberately excludes the step budget and the world size: a longer run may continue an older
    checkpoint, and the shards reassemble to the full state whatever the process count was.
    """
    blob = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _manifest_digest(body: dict[str, Any]) -> str:
    clean = {k: v for k, v in body.items() if k != "manifest_sha256"}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_manifest(
    run_dir: Path,
    *,
    step: int,
    world_size: int,
    cfg_hash: str,
    shards: list[dict[str, Any]],
    resumed_from_step: int | None,
    epoch: int = 0,
) -> Path:
    """Commit a checkpoint: ``shards`` is one ``{rank, file, sha256, bytes}`` per rank."""
    body: dict[str, Any] = {
        "format": 1,
        "step": step,
        "epoch": epoch,
        "world_size": world_size,
        "config_hash": cfg_hash,
        "resumed_from_step": resumed_from_step,
        "shards": sorted(shards, key=lambda s: s["rank"]),
    }
    body["manifest_sha256"] = _manifest_digest(body)
    path = step_dir(run_dir, step) / MANIFEST
    atomic_write_bytes(path, json.dumps(body, indent=1, sort_keys=True).encode())
    return path


@dataclass
class CheckpointStatus:
    step: int
    valid: bool
    reason: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    directory: Path | None = None


def verify_checkpoint_dir(directory: Path, cfg_hash: str | None = None) -> CheckpointStatus:
    """Check one ``step-*`` directory. Never raises: a broken checkpoint is an invalid one."""
    m = _STEP_RE.match(directory.name)
    step = int(m.group(1)) if m else -1
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
    except FileNotFoundError:
        return CheckpointStatus(step, False, "no manifest (uncommitted)", directory=directory)
    except (OSError, ValueError) as exc:
        return CheckpointStatus(step, False, f"unreadable manifest: {exc}", directory=directory)
    if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != _manifest_digest(
        manifest
    ):
        return CheckpointStatus(step, False, "manifest digest mismatch", directory=directory)
    if manifest.get("step") != step:
        return CheckpointStatus(step, False, "manifest step != directory", directory=directory)
    if cfg_hash is not None and manifest.get("config_hash") != cfg_hash:
        return CheckpointStatus(step, False, "config hash mismatch", manifest, directory)
    shards = manifest.get("shards") or []
    if len(shards) != manifest.get("world_size") or {s.get("rank") for s in shards} != set(
        range(manifest.get("world_size") or 0)
    ):
        return CheckpointStatus(step, False, "shard set incomplete", manifest, directory)
    for s in shards:
        f = directory / str(s.get("file"))
        try:
            if f.stat().st_size != s.get("bytes") or sha256_file(f) != s.get("sha256"):
                return CheckpointStatus(
                    step, False, f"shard {s.get('rank')} corrupt", manifest, directory
                )
        except OSError:
            return CheckpointStatus(
                step, False, f"shard {s.get('rank')} missing", manifest, directory
            )
    return CheckpointStatus(step, True, "", manifest, directory)


def list_checkpoints(run_dir: Path, cfg_hash: str | None = None) -> list[CheckpointStatus]:
    """Every checkpoint directory, newest step first, each verified."""
    root = Path(run_dir) / CKPT_DIR
    if not root.is_dir():
        return []
    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir() and _STEP_RE.match(d.name)), reverse=True
    )
    return [verify_checkpoint_dir(d, cfg_hash) for d in dirs]


def find_latest_valid(
    run_dir: Path, cfg_hash: str | None = None
) -> tuple[CheckpointStatus | None, list[CheckpointStatus]]:
    """The newest fully valid checkpoint, plus the newer-but-invalid ones that were skipped."""
    skipped: list[CheckpointStatus] = []
    for st in list_checkpoints(run_dir, cfg_hash):
        if st.valid:
            return st, skipped
        skipped.append(st)
    return None, skipped


def parse_metrics(text: str) -> dict[str, Any] | None:
    """The last ``EXAMLOPS_DIST_METRICS=<json>`` line in ``text``, or None."""
    found = None
    for line in text.splitlines():
        if line.startswith(METRICS_MARKER):
            try:
                found = json.loads(line[len(METRICS_MARKER) :])
            except ValueError:
                continue
    return found
