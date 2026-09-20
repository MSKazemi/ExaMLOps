"""Platform ⟂ use-case boundary tests (ADR 0094).

Guards the separation between the ExaMLOps platform and a use-case pack:
  1. the platform core / pipeline engine imports no ``seanergys_modelzoo`` directly;
  2. the loader resolves the bundled reference pack by default;
  3. a completely different (non-reference) pack loads purely from its ``pack.toml`` — proving the
     seam carries no hardcoded use-case knowledge.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import OrderedDict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipelines import usecase  # noqa: E402


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "check_usecase_boundary", _REPO_ROOT / "platform" / "ci" / "check_usecase_boundary.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_no_direct_modelzoo_imports_in_core():
    """The platform library + pipeline engine must not import the use-case model lib directly."""
    guard = _load_guard()
    violations = guard.find_violations()
    assert violations == [], "boundary violations:\n" + "\n".join(violations)


def test_default_pack_resolves_reference():
    """With no env override the loader finds the bundled reference pack's per-model YAML."""
    usecase.pack.cache_clear()
    md = usecase.models_dir()
    assert md.name == "models"
    assert (md / "jpcp.yaml").is_file()
    assert usecase.config_package() == "model_configs"


def test_second_pack_loads_from_toml(tmp_path, monkeypatch):
    """A non-reference pack loads entirely from its pack.toml — no hardcoded use-case knowledge."""
    pack = tmp_path / "acme"
    (pack / "models").mkdir(parents=True)
    (pack / "models" / "widget.yaml").write_text("name: Widget\n")
    # Framework + datasets point at stdlib objects to prove the seam is framework-agnostic.
    (pack / "pack.toml").write_text(
        "[pack]\nname = 'acme'\n\n"
        "[content]\nmodels_dir = 'models'\nconfig_package = 'model_configs'\n\n"
        "[framework]\nmodel_base = 'collections:OrderedDict'\n"
        "config_base = 'collections:OrderedDict'\n"
        "pipeline_step = 'collections:OrderedDict'\n"
        "model_params = 'collections:OrderedDict'\n"
        "dataloader = 'collections:OrderedDict'\n"
        "dataloader_params = 'collections:OrderedDict'\n"
        "get_backend = 'collections:OrderedDict'\n"
        "framework_adapter = 'collections:OrderedDict'\n"
        "model_task = 'collections:OrderedDict'\n\n"
        "[datasets]\nWidgetData = 'collections:OrderedDict'\n"
    )
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(pack))
    usecase.pack.cache_clear()

    assert usecase.usecase_dir() == pack
    assert (usecase.models_dir() / "widget.yaml").is_file()
    assert usecase.dataset_registry() == {"WidgetData": OrderedDict}
    assert usecase.framework()["model_base"] is OrderedDict
    usecase.pack.cache_clear()
