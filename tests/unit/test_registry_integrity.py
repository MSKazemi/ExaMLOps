"""Registry-integrity CI guard.

These tests catch half-applied scaffolding before merge. They cost nothing to
run (no network, no training) and they fail loudly when:

  * a config file declares a MODEL_CLASS that doesn't actually exist;
  * a config exists but no model with that MODEL_CLASS is auto-discovered;
  * a model is auto-discovered but its config does not specify a usable
    inference contract (model_id / promotion gate).

Phase 2 introduces the cookiecutter scaffold (``exa scaffold``) — this guard
is what makes that workflow safe: half-finished new models produce a clear
test failure rather than a silent platform-wide regression.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELZOO = REPO_ROOT / "modelzoo"
for p in (str(REPO_ROOT), str(MODELZOO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── upstream-library guard ───────────────────────────────────────────────────
# `seanergys_modelzoo` is an UPSTREAM library, not part of ExaMLOps (ADR 0094):
# the platform core never imports it — only the use-case pack does, through the
# loader seam. It is therefore not vendored in the public tree; CI and the
# deploy node fetch it from its own repo. Skip rather than fail when absent.
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (REPO_ROOT / "modelzoo"))
if not (_MZ / "seanergys_modelzoo").is_dir():
    pytest.skip(
        "seanergys_modelzoo not present — upstream library fetched at deploy/CI "
        "time. Set EXAMLOPS_MODELZOO_DIR to a checkout to run these tests.",
        allow_module_level=True,
    )
if str(_MZ) not in sys.path:
    sys.path.insert(0, str(_MZ))


from pipelines.pipeline_generator import MODEL_REGISTRY  # noqa: E402
from pipelines.usecase import models_dir  # noqa: E402

REQUIRED_INFERENCE_KEYS = (
    "model_id",
    "promotion_metric",
    "promotion_threshold",
    "promotion_direction",
)
VALID_DIRECTIONS = ("higher_is_better", "lower_is_better")


def test_registry_is_non_empty():
    """Sanity: at least one model must be registered."""
    assert MODEL_REGISTRY, "MODEL_REGISTRY is empty — auto-discovery failed"


@pytest.mark.parametrize("model_name", sorted(MODEL_REGISTRY.keys()))
def test_model_class_is_concrete(model_name):
    """Every registered config must point to an importable model class."""
    model_cls, config_cls, _ = MODEL_REGISTRY[model_name]
    assert model_cls is not None, f"{config_cls.__name__}.MODEL_CLASS is None"
    assert callable(model_cls), f"{model_cls!r} is not a class"


@pytest.mark.parametrize("model_name", sorted(MODEL_REGISTRY.keys()))
def test_supported_datasets_are_listed(model_name):
    """Each config must declare at least one SeanergysDataset subclass."""
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    assert config_cls.SUPPORTED_DATASETS, f"{config_cls.__name__}.SUPPORTED_DATASETS is empty"
    for ds_cls in config_cls.SUPPORTED_DATASETS:
        assert hasattr(ds_cls, "__name__"), f"non-class entry in SUPPORTED_DATASETS: {ds_cls!r}"


@pytest.mark.parametrize("model_name", sorted(MODEL_REGISTRY.keys()))
def test_inference_params_are_complete(model_name):
    """Each config must produce a complete inference contract.

    Without this, the promotion gate has no metric to test and Ray Serve has
    no schema to expose.
    """
    _, config_cls, _ = MODEL_REGISTRY[model_name]
    inf = config_cls.get_inference_params()
    assert isinstance(inf, dict), f"get_inference_params() must return a dict, got {type(inf)}"

    missing = [k for k in REQUIRED_INFERENCE_KEYS if k not in inf]
    assert not missing, f"{config_cls.__name__}: inference_params missing keys: {missing}"

    assert inf["promotion_direction"] in VALID_DIRECTIONS, (
        f"{config_cls.__name__}: promotion_direction must be one of {VALID_DIRECTIONS}, "
        f"got {inf['promotion_direction']!r}"
    )

    assert isinstance(inf["promotion_threshold"], (int, float)), (
        f"{config_cls.__name__}: promotion_threshold must be numeric"
    )

    assert inf["model_id"], f"{config_cls.__name__}: model_id is empty"


@pytest.mark.parametrize("model_name", sorted(MODEL_REGISTRY.keys()))
def test_get_train_components_signature_accepts_backend(model_name):
    """Phase 1 requires every config to accept the ``backend_name`` kwarg.

    This protects the auto-generated cookiecutter template — and any
    hand-written config — from regressing the dataset-backend abstraction.
    """
    import inspect

    _, config_cls, _ = MODEL_REGISTRY[model_name]
    sig = inspect.signature(config_cls.get_train_components)
    assert "backend_name" in sig.parameters, (
        f"{config_cls.__name__}.get_train_components must accept backend_name kwarg "
        f"(Phase 1 dataset backend abstraction); got params: {list(sig.parameters)}"
    )


def test_no_duplicate_model_ids():
    """Two configs registering the same MLflow model_id would race in promotion."""
    seen: dict[str, str] = {}
    for model_name, (_, config_cls, _) in MODEL_REGISTRY.items():
        model_id = config_cls.get_inference_params().get("model_id")
        if model_id in seen:
            pytest.fail(
                f"duplicate inference model_id={model_id!r} between "
                f"{seen[model_id]} and {model_name}"
            )
        seen[model_id] = model_name


# ── Phase 14: Per-model YAML file guards ──────────────────────────────────────

# Active use-case pack's model dir (ADR 0094); previously the in-tree ``pipelines/models``,
# which now resolves empty and would make this integrity guard pass vacuously.
_MODELS_DIR = models_dir()

REQUIRED_YAML_FIELDS = (
    "name",
    "config_class",
    "task_type",
    "lifecycle",
    "serving",
    "prefect",
    "inference",
)


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_has_required_top_level_fields(yaml_path):
    from pipelines.model_loader import load_model_yaml

    cfg = load_model_yaml(yaml_path)
    assert cfg.name, f"{yaml_path.name}: name is empty"
    assert cfg.config_class, f"{yaml_path.name}: config_class is empty"
    assert cfg.task_type in ("regression", "classification"), (
        f"{yaml_path.name}: task_type must be 'regression' or 'classification', got {cfg.task_type!r}"
    )
    assert cfg.lifecycle, f"{yaml_path.name}: lifecycle is empty"
    assert cfg.serving.get("model_id"), f"{yaml_path.name}: serving.model_id is missing"
    assert cfg.prefect.get("schedule"), f"{yaml_path.name}: prefect.schedule is missing"
    assert cfg.inference.get("input_schema"), f"{yaml_path.name}: inference.input_schema is missing"
    assert cfg.inference.get("output_schema"), (
        f"{yaml_path.name}: inference.output_schema is missing"
    )


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_engine_block_is_valid(yaml_path):
    """A malformed ``engine:`` block must fail CI (GWT-2, ADR 0016/0107).

    ``validate_engine_block`` always claimed to back "the registry-integrity CI guard" but
    nothing here called it, so a bad block — including a vision model with no per-prompt
    media limit — could reach a serving host unchallenged. This is that guard.
    """
    import yaml as _yaml

    from examlops.engines import validate_engine_block

    doc = _yaml.safe_load(yaml_path.read_text()) or {}
    block = doc.get("engine")
    if block is None:
        return  # no engine block ⇒ defaults apply, nothing to validate
    errors = validate_engine_block(block)
    assert errors == [], f"{yaml_path.name}: invalid engine block: {errors}"


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_fairness_block_is_valid(yaml_path):
    """A malformed ``fairness:`` block must fail CI (ADR 0025 clause 1).

    The failure this guards against is quiet: ``slice:`` for ``slices:`` yields a registry that
    declares no attributes, and a fairness gate over zero attributes passes every model. A
    protection that silently does not apply is worse than one that was never configured.
    """
    import yaml as _yaml

    from examlops.fairness import validate_fairness_block

    doc = _yaml.safe_load(yaml_path.read_text()) or {}
    block = doc.get("fairness")
    if block is None:
        return  # no fairness block ⇒ no slice registry declared, nothing to validate
    errors = validate_fairness_block(block, model=str(doc.get("name", yaml_path.stem)))
    assert errors == [], f"{yaml_path.name}: invalid fairness block: {errors}"


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_fairness_block_survives_the_loader(yaml_path):
    """The block must reach ``ModelYAMLConfig`` — the ``engine:`` key was silently dropped once."""
    import yaml as _yaml

    from pipelines.model_loader import load_model_yaml

    raw = (_yaml.safe_load(yaml_path.read_text()) or {}).get("fairness") or {}
    assert load_model_yaml(yaml_path).fairness == raw


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_lifecycle_stages_have_valid_directions(yaml_path):
    from pipelines.model_loader import load_model_yaml

    cfg = load_model_yaml(yaml_path)
    for stage in cfg.lifecycle:
        assert stage.get("direction") in VALID_DIRECTIONS, (
            f"{yaml_path.name}: stage {stage.get('name')!r} has invalid direction {stage.get('direction')!r}"
        )
        assert isinstance(stage.get("threshold"), (int, float)), (
            f"{yaml_path.name}: stage {stage.get('name')!r} threshold must be numeric"
        )


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_config_class_resolves(yaml_path):
    from pipelines.model_loader import load_model_yaml
    from pipelines.pipeline_generator import _import_shim

    cfg = load_model_yaml(yaml_path)
    shim = _import_shim(cfg.config_class)
    assert shim.MODEL_CLASS is not None, f"{yaml_path.name}: shim.MODEL_CLASS is None"
    assert shim.SUPPORTED_DATASETS, f"{yaml_path.name}: shim.SUPPORTED_DATASETS is empty"


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_datasets_match_shim_supported_datasets(yaml_path):
    from pipelines.model_loader import load_model_yaml
    from pipelines.pipeline_generator import _import_shim

    cfg = load_model_yaml(yaml_path)
    shim = _import_shim(cfg.config_class)
    shim_ds_names = {d.__name__ for d in shim.SUPPORTED_DATASETS}
    yaml_ds_names = {d.name for d in cfg.datasets}
    missing = yaml_ds_names - shim_ds_names
    assert not missing, (
        f"{yaml_path.name}: datasets in YAML not in shim.SUPPORTED_DATASETS: {missing}"
    )


def test_no_duplicate_yaml_model_ids():
    from pipelines.model_loader import scan_model_yamls

    configs = scan_model_yamls(_MODELS_DIR)
    seen: dict[str, str] = {}
    for cfg in configs:
        mid = cfg.serving.get("model_id", "")
        if mid in seen:
            pytest.fail(f"Duplicate model_id={mid!r} in {seen[mid]} and {cfg.name}")
        seen[mid] = cfg.name


@pytest.mark.parametrize("yaml_path", sorted(_MODELS_DIR.glob("*.yaml")))
def test_yaml_dataplane_binding_is_valid(yaml_path):
    """ADR 0130: backend: dataplane without a binding would fail at training time — fail here."""
    from pipelines.datasets.dataplane import DataplaneBinding
    from pipelines.model_loader import load_model_yaml

    cfg = load_model_yaml(yaml_path)
    for ds in cfg.datasets:
        binding = DataplaneBinding.from_yaml(ds.dataplane)  # raises on a malformed block
        if ds.backend == "dataplane":
            assert binding is not None, (
                f"{cfg.name}/{ds.name}: backend dataplane needs datasets[].dataplane.source"
            )
