"""Per-model YAML loader for ExaMLOps.

Each model has its own YAML file at the active pack's models/<name>.yaml.
This module parses those files into typed dataclasses consumed by
pipeline_generator.py and other system components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
    seanerbus_uuid: str | None = None
    project: str | None = None  # owning Project (ADR 0088), optional default membership

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
        seanerbus_uuid=raw.get("seanerbus_uuid") or None,
        project=raw.get("project") or None,
    )


def scan_model_yamls(models_dir: Path) -> list[ModelYAMLConfig]:
    """Scan a directory for *.yaml files and return parsed configs (alphabetical order)."""
    return [
        load_model_yaml(f) for f in sorted(models_dir.glob("*.yaml")) if not f.stem.startswith("_")
    ]
