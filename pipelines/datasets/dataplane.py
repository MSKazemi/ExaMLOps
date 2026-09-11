"""Training-side dataplane adapter (ADR 0130 §8): resolve once, pin once.

A run resolves one revision per (model, dataset) the first time a dataset is built, downloads it
once, and every later build in the same process — train, validation, the contract gate, the MLflow
tag — reuses that pin, even if the source's ``_latest`` moves meanwhile.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


class LogicalPathNotInSnapshot(KeyError):
    """A dataset asked for a file the pinned snapshot has no table for."""


@dataclass(frozen=True)
class DataplaneBinding:
    source: str
    project: str = ""
    revision: str = "latest"
    tables: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, raw: dict[str, Any] | None) -> DataplaneBinding | None:
        if not raw:
            return None
        if not isinstance(raw, dict) or not raw.get("source"):
            raise ValueError("datasets[].dataplane needs a 'source' (the dataplane source name)")
        tables = raw.get("tables") or {}
        if not isinstance(tables, dict):
            raise ValueError("datasets[].dataplane.tables must map logical paths to table names")
        return cls(
            source=str(raw["source"]),
            project=str(raw.get("project") or ""),
            revision=str(raw.get("revision") or "latest"),
            tables={str(k): str(v) for k, v in tables.items()},
        )


@dataclass(frozen=True)
class SnapshotPin:
    source_key: str
    revision: str
    local_dir: Path
    tables: tuple[str, ...]
    manifest_uri: str


_PINS: dict[tuple[str, str], SnapshotPin] = {}
_LOCK = threading.Lock()


def cache_root() -> Path:
    """Where pinned snapshots are materialized: the explicit cache dir, else the ADR 0128 data
    root, else the user cache dir — never the checkout (an HPC node may not have one)."""
    if explicit := os.getenv("EXAMLOPS_DATAPLANE_CACHE_DIR", "").strip():
        return Path(explicit).expanduser()
    if root := os.getenv("EXAMLOPS_DATA_DIR", "").strip():
        return Path(root).expanduser() / "cache" / "dataplane"
    xdg = os.getenv("XDG_CACHE_HOME", "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "examlops" / "dataplane"


def pin_for(
    model_name: str, dataset_name: str, binding: DataplaneBinding, *, project: str = ""
) -> SnapshotPin:
    key = (model_name.upper(), dataset_name)
    with _LOCK:
        if key in _PINS:
            return _PINS[key]
        from examlops.dataplane import (
            materialize,
            read_manifest,
            resolve,
            source_key,
            store_from_env,
        )

        revision = os.getenv("EXAMLOPS_DATASET_REVISION", "").strip() or binding.revision
        store = store_from_env()
        skey = source_key(binding.project or project, binding.source)
        ref = resolve(store, skey, revision)
        local = materialize(store, ref, cache_root())
        manifest = read_manifest(store, ref)
        pin = SnapshotPin(skey, ref.revision, local, manifest.tables, store.uri(ref.manifest_key))
        _PINS[key] = pin
        print(f"[dataplane] {model_name} x {dataset_name} pinned to {skey}@{ref.revision[:12]}")
        return pin


def current_pin(model_name: str | None, dataset_name: str) -> SnapshotPin | None:
    if not model_name:
        return None
    return _PINS.get((model_name.upper(), dataset_name))


def reset_pins() -> None:
    with _LOCK:
        _PINS.clear()


def forget_pin(model_name: str, dataset_name: str) -> None:
    """Drop one (model, dataset) pin so the next build resolves afresh.

    The training flow calls this once at its start: every build inside one run shares the pin,
    but a second run in the same process never inherits the first run's snapshot.
    """
    with _LOCK:
        _PINS.pop((model_name.upper(), dataset_name), None)


class DataplaneDatasetBackend:
    """Satisfies modelzoo's ``DatasetBackend`` protocol structurally (``name`` + ``fetch``)."""

    name = "dataplane"

    def __init__(self, pin: SnapshotPin, tables: dict[str, str]) -> None:
        self.pin = pin
        self.tables = tables

    def fetch(self, logical_path: str, cache_dir: Path) -> Path:
        table = self.tables.get(logical_path) or PurePosixPath(logical_path).stem
        directory = self.pin.local_dir / table
        parts = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
        if not parts:
            raise LogicalPathNotInSnapshot(
                f"{logical_path!r} maps to table {table!r}, which is not in "
                f"{self.pin.source_key}@{self.pin.revision[:12]} (tables: {list(self.pin.tables)}); "
                "map it with datasets[].dataplane.tables"
            )
        return parts[0] if len(parts) == 1 else directory
