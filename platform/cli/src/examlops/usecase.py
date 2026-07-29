"""Use-case pack location for the platform CLI (ADR 0094).

The ExaMLOps platform is use-case-agnostic: it does not know about any concrete model or dataset,
only *where* the active use-case pack keeps its per-model YAML. That location is resolved from the
environment, so a deployment points the platform at its own pack without any code change:

    RAY_MODELS_DIR / MODELS_YAML_DIR   explicit per-model YAML dir (highest precedence)
    EXAMLOPS_USECASE_DIR               pack root; YAML dir is ``<root>/models``
    installed pack (entry point)       a pip-installed pack registered under
                                       ``examlops.usecase_packs`` (Stage 5, ADR 0094)
    (default)                          the bundled reference pack, ``usecases/seanergy/models``

This is a thin path resolver only — it imports nothing from the pipeline engine or the pack,
and discovers installed packs *generically* by group, never naming a concrete use-case.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MODELS_DIR = "usecases/seanergy/models"

#: Entry-point group a graduated, pip-installable use-case pack registers under. Each entry
#: point resolves to the pack root directory (a ``Path``/``str``) or a zero-arg callable
#: returning it. Keeps the platform use-case-agnostic: it finds *a* pack, never names one.
USECASE_PACK_GROUP = "examlops.usecase_packs"


def _entry_point_pack_root() -> Path | None:
    """Discover an installed use-case pack via the ``examlops.usecase_packs`` entry point.

    Returns the first pack whose root exists, or None when none is installed / discovery
    fails. Fail-open: a broken pack entry point never crashes path resolution.
    """
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group=USECASE_PACK_GROUP)
    except Exception:
        return None
    for ep in eps:
        try:
            target = ep.load()
            root = target() if callable(target) else target
            path = Path(root)
        except Exception:
            continue
        if path.is_dir():
            return path
    return None


def models_dir(default: str = DEFAULT_MODELS_DIR) -> Path:
    """Resolve the active pack's per-model YAML directory (see module docstring for precedence)."""
    for var in ("RAY_MODELS_DIR", "MODELS_YAML_DIR"):
        value = os.getenv(var)
        if value:
            return Path(value)
    pack_root = os.getenv("EXAMLOPS_USECASE_DIR")
    if pack_root:
        return Path(pack_root) / "models"
    ep_pack = _entry_point_pack_root()
    if ep_pack is not None:
        return ep_pack / "models"
    return Path(default)


def default_dataset_for(model: str) -> str | None:
    """First dataset name declared in the model's per-model YAML (pack), or None.

    Lets platform commands resolve a sensible default dataset from the *pack's* config instead
    of hardcoding a use-case dataset name.
    """
    path = models_dir() / f"{model.lower()}.yaml"
    if not path.is_file():
        return None
    try:
        import yaml

        doc = yaml.safe_load(path.read_text()) or {}
        datasets = doc.get("datasets") or []
        if datasets:
            return datasets[0].get("name")
    except Exception:
        return None
    return None


def _pack_dir() -> Path | None:
    root = os.getenv("EXAMLOPS_USECASE_DIR")
    if root:
        return Path(root)
    ep_pack = _entry_point_pack_root()
    if ep_pack is not None:
        return ep_pack
    md = models_dir()
    # models_dir is "<pack>/models" by convention.
    return md.parent if md.name == "models" else None


def dataset_schema(dataset: str) -> list[dict] | None:
    """Field schema for a dataset, read from the pack's ``datasets/schemas.json`` (or None).

    Keeps concrete dataset schemas (e.g. FData columns) in the use-case pack, not the platform.
    """
    pack = _pack_dir()
    if pack is None:
        return None
    schema_file = pack / "datasets" / "schemas.json"
    if not schema_file.is_file():
        return None
    try:
        import json

        data = json.loads(schema_file.read_text())
        fields = data.get(dataset)
        return fields if isinstance(fields, list) else None
    except Exception:
        return None
