"""Per-model YAML loader for ExaMLOps.

Each model has its own YAML file at the active pack's models/<name>.yaml.
This module parses those files into typed dataclasses consumed by
pipeline_generator.py and other system components.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Env gate for the legacy dataplane_bus_uuid -> stream shim (E15). Default OFF, mirrors
#: examlops.dataplane.streams.bindings._LEGACY_ENV — kept as a literal here, not imported, because
#: this module runs on HPC nodes that may not carry the platform package at all.
_LEGACY_DATAPLANE_BUS_ENV = "EXAMLOPS_DATAPLANE_LEGACY_DATAPLANE_BUS_UUID"

#: Mirrors examlops.dataplane.streams.types.StreamLimits' field defaults. Duplicated (not
#: imported — see module docstring) so a stream entry with no ``limits`` block normalizes to the
#: same values on both sides; test_dataplane_streams_catalog.py's parity test is the drift guard.
_DEFAULT_STREAM_LIMITS: dict[str, Any] = {
    "max_in_flight": 64,
    "rate_per_min": 0,
    "deadline_ms": None,
    "max_bytes": 1_048_576,
    "max_attempts": 5,
}


def _use_legacy_dataplane_bus_shim() -> bool:
    return (os.getenv(_LEGACY_DATAPLANE_BUS_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SplitConfig:
    files: list[str] = field(default_factory=list)
    filters: list[list] = field(default_factory=list)


@dataclass
class DatasetEntry:
    name: str
    backend: str = "zenodo"
    cache_dir: str = ""
    batch_size: int = 1
    columns: list[str] = field(default_factory=list)
    input_features: list[str] = field(default_factory=list)
    output_features: list[str] = field(default_factory=list)
    splits: dict[str, SplitConfig] = field(default_factory=dict)
    # ADR 0130: bind this dataset entry to a dataplane source (only used with backend: dataplane).
    # Kept raw; pipelines.datasets.dataplane.DataplaneBinding validates it.
    dataplane: dict[str, Any] | None = None


@dataclass
class ModelYAMLConfig:
    name: str
    config_class: str
    task_type: str
    framework: str = "sklearn"
    enabled: bool = True
    model_class: str = ""
    model: dict[str, Any] = field(default_factory=dict)
    datasets: list[DatasetEntry] = field(default_factory=list)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    serving: dict[str, Any] = field(default_factory=dict)
    prefect: dict[str, Any] = field(default_factory=dict)
    inference: dict[str, Any] = field(default_factory=dict)
    dataplane_bus_uuid: str | None = None
    project: str | None = None  # owning Project (ADR 0088), optional default membership
    # Inference-engine block (ADR 0016/0107). Kept as a raw mapping: it is validated by
    # examlops.engines.validate_engine_block (registry-integrity CI guard) and consumed by
    # to_vllm_args / the KServe manifest generator. Previously this key was silently
    # dropped here, so the engine block could never reach the Ray Serve loader.
    engine: dict[str, Any] = field(default_factory=dict)
    # Fairness slice registry (ADR 0025 clause 1). Kept as a raw mapping and validated by
    # examlops.fairness.validate_fairness_block (registry-integrity CI guard). Declaring the
    # protected/binned attributes here rather than only in `fairness_config` puts them in code
    # review and in the deployment, instead of in a runtime table that a fresh database loses.
    fairness: dict[str, Any] = field(default_factory=dict)

    def dataset(self, name: str) -> DatasetEntry:
        for ds in self.datasets:
            if ds.name == name:
                return ds
        raise KeyError(f"Dataset {name!r} not found in model {self.name!r}")

    def split_config(self, dataset_name: str, split: str) -> SplitConfig:
        ds = self.dataset(dataset_name)
        if split in ds.splits:
            return ds.splits[split]
        if split == "test" and "validation" in ds.splits:
            return ds.splits["validation"]
        raise KeyError(
            f"Split {split!r} not found for dataset {dataset_name!r} in model {self.name!r}"
        )


def _parse_split(raw: dict | None) -> SplitConfig:
    if not raw:
        return SplitConfig()
    return SplitConfig(
        files=[str(f) for f in raw.get("files", [])],
        filters=[list(f) for f in raw.get("filters", [])],
    )


def _parse_dataset(raw: dict) -> DatasetEntry:
    splits = {k: _parse_split(v) for k, v in raw.get("splits", {}).items()}
    return DatasetEntry(
        name=raw["name"],
        backend=raw.get("backend", "zenodo"),
        cache_dir=raw.get("cache_dir", ""),
        batch_size=raw.get("batch_size", 1),
        columns=list(raw.get("columns", [])),
        input_features=list(raw.get("input_features", [])),
        output_features=list(raw.get("output_features", [])),
        splits=splits,
        dataplane=raw.get("dataplane") or None,
    )


def load_model_yaml(path: Path) -> ModelYAMLConfig:
    """Parse a single per-model YAML file into a ModelYAMLConfig."""
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    return ModelYAMLConfig(
        name=raw["name"],
        config_class=raw["config_class"],
        task_type=raw["task_type"],
        framework=raw.get("framework", "sklearn"),
        enabled=raw.get("enabled", True),
        model_class=raw.get("model_class", ""),
        model=raw.get("model", {}),
        datasets=[_parse_dataset(d) for d in raw.get("datasets", [])],
        lifecycle=raw.get("lifecycle", []),
        serving=raw.get("serving", {}),
        prefect=raw.get("prefect", {}),
        inference=raw.get("inference", {}),
        dataplane_bus_uuid=raw.get("dataplane_bus_uuid") or None,
        project=raw.get("project") or None,
        engine=raw.get("engine") or {},
        fairness=raw.get("fairness") or {},
    )


def scan_model_yamls(models_dir: Path) -> list[ModelYAMLConfig]:
    """Scan a directory for *.yaml files and return parsed configs (alphabetical order)."""
    return [
        load_model_yaml(f) for f in sorted(models_dir.glob("*.yaml")) if not f.stem.startswith("_")
    ]


def normalize_inference_streams(model_yaml: ModelYAMLConfig) -> list[dict[str, Any]]:
    """Normalize ``inference.streams`` (+ the legacy ``dataplane_bus_uuid`` shim) into a list of plain
    dicts with defaults applied.

    This is the pure, ``examlops.dataplane``-import-free twin of
    ``examlops.dataplane.streams.bindings.yaml_streams`` — this module runs on HPC nodes, which may
    not carry the platform package (``examlops``) at all, only ``examlops-pipelines``. The two are
    kept in agreement by ``tests/unit/test_dataplane_streams_catalog.py``'s parity test.

    Each dict has keys: ``name``, ``connector``, ``model``, ``alias``, ``address``, ``connection``,
    ``options``, ``limits`` — no ``project`` (this module has no notion of tenancy; that is a
    dataplane-layer concern one level up) and no ``state``/``origin`` (catalog-only concerns).
    """
    model = model_yaml.name
    raw_streams = (model_yaml.inference or {}).get("streams") or []
    out: list[dict[str, Any]] = []
    for entry in raw_streams:
        name = entry.get("name")
        connector = entry.get("connector")
        if not name or not connector:
            raise ValueError(
                f"inference.streams entry for model {model!r} needs 'name' and 'connector'"
            )
        raw_limits = entry.get("limits") or {}
        out.append(
            {
                "name": str(name),
                "connector": str(connector),
                "model": model,
                "alias": entry.get("alias") or "Production",
                "address": entry.get("address") or "",
                "connection": entry.get("connection"),
                "options": dict(entry.get("options") or {}),
                "limits": {
                    **_DEFAULT_STREAM_LIMITS,
                    **{k: v for k, v in raw_limits.items() if k in _DEFAULT_STREAM_LIMITS},
                },
            }
        )
    if (
        _use_legacy_dataplane_bus_shim()
        and model_yaml.dataplane_bus_uuid
        and not any(s["connector"] == "dataplane-bus" for s in out)
    ):
        out.append(
            {
                "name": f"{model}-dataplane-bus",
                "connector": "dataplane-bus",
                "model": model,
                "alias": "Production",
                "address": str(model_yaml.dataplane_bus_uuid),
                "connection": None,
                "options": {},
                "limits": dict(_DEFAULT_STREAM_LIMITS),
            }
        )
    return out
