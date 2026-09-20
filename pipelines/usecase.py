"""Use-case pack loader — the platform/use-case seam (ADR 0094).

A *use-case pack* is a directory (default: ``usecases/reference``) holding one deployment's
content — per-model YAML, transform shims, and a ``pack.toml`` that declares where its models,
config package, datasets and ML-framework base-classes live. The ExaMLOps pipeline engine loads
**everything** through this module so the platform core names no concrete model, dataset, or
framework. Point the platform at a different pack with ``EXAMLOPS_USECASE_DIR``.

Everything degrades to the legacy in-tree reference layout when no pack is present, so the platform
keeps working mid-migration.
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any

try:  # py>=3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 fallback
    import tomli as tomllib  # type: ignore

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PACK = _REPO_ROOT / "usecases" / "reference"

# Legacy fallbacks (pre-ADR-0094 in-tree layout) so a partial migration never breaks discovery.
_LEGACY_MODELS = _REPO_ROOT / "pipelines" / "models"
_LEGACY_CONFIG_PKG = "pipelines.model_configs"
_LEGACY_FRAMEWORK: dict[str, str] = {
    "model_base": "seanergys_modelzoo.models.common.seanergys_model:SeanergysModel",
    "config_base": (
        "seanergys_modelzoo.models.common.seanergys_configurator:SeanergysModelConfiguration"
    ),
    "pipeline_step": "seanergys_modelzoo.decorators:pipeline_step",
    "model_params": "seanergys_modelzoo.models.common.seanergys_configurator:SeanergysModelParams",
    "dataloader": "seanergys_modelzoo.dataloader.seanergys_dataloader:SeanergysDataloader",
    "dataloader_params": (
        "seanergys_modelzoo.models.common.seanergys_configurator:SeanergysDataloaderParams"
    ),
    "get_backend": "seanergys_modelzoo.datasets._backends:get_backend",
    "framework_adapter": "seanergys_modelzoo.models.common.framework_adapter:adapter_for",
    "model_task": "seanergys_modelzoo.models.common.seanergys_model:SeanergysModelTask",
    "tasks_root": "seanergys_modelzoo.models.tasks",
}
_LEGACY_DATASETS = {
    "PM100Dataset": "seanergys_modelzoo.datasets.pm100:PM100Dataset",
    "FDataDataset": "seanergys_modelzoo.datasets.f_data:FDataDataset",
}


def usecase_dir() -> Path:
    """Resolve the active pack directory.

    ``EXAMLOPS_USECASE_DIR`` → the site's pack in the instance-data root
    (``$EXAMLOPS_DATA_DIR/usecase``, ADR 0128) → the bundled default pack → the legacy tree.
    """
    env = os.getenv("EXAMLOPS_USECASE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    data_root = os.getenv("EXAMLOPS_DATA_DIR", "").strip()
    if data_root:
        site_pack = Path(data_root).expanduser() / "usecase"
        if (site_pack / "pack.toml").is_file():
            return site_pack.resolve()
    if (_DEFAULT_PACK / "pack.toml").exists():
        return _DEFAULT_PACK
    return _REPO_ROOT / "pipelines"  # pre-migration fallback


@cache
def pack() -> dict[str, Any]:
    """Parse ``pack.toml`` (empty dict in legacy mode) and bootstrap the pack's ``sys.path``."""
    root = usecase_dir()
    cfg: dict[str, Any] = {}
    toml = root / "pack.toml"
    if toml.exists():
        with open(toml, "rb") as fh:
            cfg = tomllib.load(fh)
    _bootstrap_syspath(root, cfg)
    return cfg


def _bootstrap_syspath(root: Path, cfg: dict[str, Any]) -> None:
    entries = [str(root), str(_REPO_ROOT)]
    for rel in cfg.get("pack", {}).get("pythonpath", []) or []:
        entries.append(str((root / rel).resolve()))
    # Upstream library (ADR 0094). Not vendored in the public tree — CI and the deploy
    # node fetch it; EXAMLOPS_MODELZOO_DIR points at that checkout when it is elsewhere.
    mz = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or _REPO_ROOT / "modelzoo")
    if mz.is_dir():
        entries.append(str(mz))
    for p in entries:
        if p not in sys.path:
            sys.path.insert(0, p)


def _resolve(spec: str) -> Any:
    """Resolve a ``"module:attr"`` (or bare ``"module"``) spec to the object."""
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr) if attr else module


def models_dir() -> Path:
    """Directory of per-model YAML files for the active pack."""
    root = usecase_dir()
    rel = pack().get("content", {}).get("models_dir", "models")
    cand = root / rel
    if cand.is_dir():
        return cand
    return _LEGACY_MODELS


def config_package() -> str:
    """Importable package name for the pack's transform shims (pack root is on ``sys.path``)."""
    cfg = pack()
    name = cfg.get("content", {}).get("config_package")
    if name and (usecase_dir() / name).is_dir():
        return name
    return _LEGACY_CONFIG_PKG


def config_dir() -> Path:
    """Filesystem dir of the config shims (for scanning)."""
    root = usecase_dir()
    name = pack().get("content", {}).get("config_package", "model_configs")
    cand = root / name
    if cand.is_dir():
        return cand
    return _REPO_ROOT / "pipelines" / "model_configs"


def framework() -> dict[str, Any]:
    """Resolve the pack's ML-framework bindings (base classes, params, dataloader, get_backend)."""
    cfg = pack()
    specs = {**_LEGACY_FRAMEWORK, **cfg.get("framework", {})}
    # tasks_root is a scan target (handled by tasks_dir), not a framework object.
    specs.pop("tasks_root", None)
    return {key: _resolve(spec) for key, spec in specs.items()}


def tasks_dir() -> Path:
    """Directory scanned for concrete model classes (the pack's ``tasks_root`` package)."""
    spec = pack().get("framework", {}).get("tasks_root", _LEGACY_FRAMEWORK["tasks_root"])
    module = importlib.import_module(spec.split(":", 1)[0])
    return Path(next(iter(module.__path__)))


def dataset_registry() -> dict[str, type]:
    """Map dataset name (as used in per-model YAML) → dataset class, from the pack."""
    specs = pack().get("datasets") or _LEGACY_DATASETS
    return {name: _resolve(spec) for name, spec in specs.items()}
