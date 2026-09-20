"""scaffold_model — generate a new SeanergysModel from the template tree.

Adds three files to the repo:

  modelzoo/seanergys_modelzoo/models/tasks/<task>/<name_lower>/
      __init__.py
      <name_lower>_model.py

  pipelines/model_configs/<name_lower>_config.py

  tests/unit/test_<name_lower>.py

Run via the Makefile (preferred):

  exa scaffold DemoAD --task anomaly_detection --type classification

Defaults match the cookiecutter.json template-defaults so that
``exa scaffold DemoAD`` Just Works for the common case.

Stdlib only — no cookiecutter dependency. Templates use ``__TOKEN__`` rather
than Jinja so the templates themselves are valid Python and can be linted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid as _uuid_mod
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "tools" / "model_template"


# ── Template substitution ────────────────────────────────────────────────────


def _classification_target_block() -> str:
    return (
        "# FData pclass label mapping: \"memory-bound\" → 0, \"compute-bound\" → 1\n"
        "_PCLASS_MAP = {\"memory-bound\": 0.0, \"compute-bound\": 1.0}\n\n\n"
        "def _pclass_target_transform(y):\n"
        "    \"\"\"Convert string pclass label (shape (1,)) to a scalar float class index.\"\"\"\n"
        "    import numpy as np  # noqa: F401\n"
        "    return _PCLASS_MAP.get(str(y[0]), -1.0)\n"
    )


def _regression_target_block() -> str:
    return (
        "# Regression target — scalar reduction from raw output features.\n\n"
        "def _scalar_target_transform(f):\n"
        "    \"\"\"Map raw target tuple to a single scalar (override for your model).\"\"\"\n"
        "    import numpy as np\n"
        "    return float(np.mean(f) if hasattr(f, \"__iter__\") else f)\n"
    )


def _render(text: str, **subs: str) -> str:
    # Tokens are wrapped in <<...>> so they can't collide with Python dunders
    # like __name__ / __init__ that appear naturally in template code.
    for key, value in subs.items():
        text = text.replace(f"<<{key}>>", value)
    return text


# ── File writers ─────────────────────────────────────────────────────────────


def _write(path: Path, content: str, *, force: bool, write_root: Path = REPO_ROOT) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (pass --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  + {path.relative_to(write_root)}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    defaults = json.loads((TEMPLATE_DIR / "cookiecutter.json").read_text())
    defaults.pop("_doc", None)

    parser = argparse.ArgumentParser(description="Scaffold a new SeanergysModel.")
    parser.add_argument("--name", required=True, help="PascalCase model name (e.g. DemoAD)")
    parser.add_argument(
        "--task",
        default=defaults["task"],
        choices=["performance_prediction", "power_consumption_prediction", "anomaly_detection"],
    )
    parser.add_argument(
        "--task-type",
        default=defaults["task_type"],
        choices=["classification", "regression"],
    )
    parser.add_argument("--estimator-import", default=defaults["estimator_import"])
    parser.add_argument("--estimator", default=defaults["estimator"])
    parser.add_argument("--promotion-metric", default=defaults["promotion_metric"])
    parser.add_argument("--promotion-threshold", type=float, default=defaults["promotion_threshold"])
    parser.add_argument(
        "--promotion-direction",
        default=defaults["promotion_direction"],
        choices=["higher_is_better", "lower_is_better"],
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument(
        "--skip-model-class", action="store_true",
        help="Only generate YAML + config shim — skip model class and test files (for existing models)",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="Override repo root for file writing (used by dashboard container)",
    )
    parser.add_argument(
        "--stdout-json", action="store_true",
        help="Print {relative_path: content} JSON to stdout instead of writing files",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", args.name):
        print(f"Error: --name must be PascalCase (letters and digits only, starting with uppercase). Got: {args.name!r}", file=sys.stderr)
        return 1

    name = args.name
    name_lower = name.lower()
    task_enum = "CLASSIFICATION" if args.task_type == "classification" else "REGRESSION"

    if args.task_type == "classification":
        target_block = _classification_target_block()
        target_transform = "_pclass_target_transform"
        output_features = "\"pclass\""
        output_key = "pclass"
        output_type = "int"
    else:
        target_block = _regression_target_block()
        target_transform = "_scalar_target_transform"
        output_features = "\"avgpcon\""
        output_key = "avgpcon"
        output_type = "float"

    subs = {
        "NAME": name,
        "name": name_lower,
        "TASK": args.task,
        "TASK_ENUM": task_enum,
        "ESTIMATOR_IMPORT": args.estimator_import,
        "ESTIMATOR": args.estimator,
        "PROMOTION_METRIC": args.promotion_metric,
        "PROMOTION_THRESHOLD": str(args.promotion_threshold),
        "PROMOTION_DIRECTION": args.promotion_direction,
        "TARGET_TRANSFORM_BLOCK": target_block,
        "TARGET_TRANSFORM": target_transform,
        "OUTPUT_FEATURES": output_features,
        "OUTPUT_KEY": output_key,
        "OUTPUT_TYPE": output_type,
    }

    # Determine write root — allows docker container to redirect writes to /repo.
    write_root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT

    model_dir = (
        write_root
        / "modelzoo"
        / "seanergys_modelzoo"
        / "models"
        / "tasks"
        / args.task
        / name_lower
    )
    model_file = model_dir / f"{name_lower}_model.py"
    init_file = model_dir / "__init__.py"
    config_file = write_root / "pipelines" / "model_configs" / f"{name_lower}_config.py"
    test_file = write_root / "tests" / "unit" / f"test_{name_lower}.py"
    yaml_file = write_root / "pipelines" / "models" / f"{name_lower}.yaml"

    # Render all file contents once (shared between --stdout-json and write paths).
    model_content = _render((TEMPLATE_DIR / "model.py.tmpl").read_text(), **subs)
    init_content = ""
    config_content = _render((TEMPLATE_DIR / "config.py.tmpl").read_text(), **subs)
    test_content = _render((TEMPLATE_DIR / "test_unit.py.tmpl").read_text(), **subs)
    yaml_content = (
        f"name: {name}\n"
        f"model_class: {name}\n"
        f"config_class: {name_lower}_config.{name}Configuration\n"
        f"task_type: {args.task_type}\n"
        f"framework: sklearn\n"
        f"enabled: true\n"
        f"dataplane_bus_uuid: {_uuid_mod.uuid4()}\n"
        f"\n"
        f"model:\n"
        f"  embedding_type: NONE\n"
        f"  hyperparameters:\n"
        f"    n_jobs: -1\n"
        f"\n"
        f"datasets:\n"
        f"  - name: FDataDataset\n"
        f"    backend: zenodo\n"
        f"    cache_dir: .data_cache/fdata\n"
        f"    batch_size: 1\n"
        f"    input_features: [embedding]\n"
        f"    output_features: [{output_key}]\n"
        f"    splits:\n"
        f"      train:\n"
        f'        files: ["23_12", "24_01"]\n'
        f"        filters:\n"
        f'          - [adt, ">=", "2023-12-01"]\n'
        f'          - [adt, "<=", "2024-01-31"]\n'
        f"      validation:\n"
        f'        files: ["24_02"]\n'
        f"        filters:\n"
        f'          - [adt, ">=", "2024-02-01"]\n'
        f'          - [adt, "<=", "2024-02-28"]\n'
        f"\n"
        f"lifecycle:\n"
        f"  - {{name: Staging,    metric: {args.promotion_metric}, threshold: {args.promotion_threshold * 0.8:.2f}, direction: {args.promotion_direction}}}\n"
        f"  - {{name: Canary,     metric: {args.promotion_metric}, threshold: {args.promotion_threshold * 0.9:.2f}, direction: {args.promotion_direction}}}\n"
        f"  - {{name: Production, metric: {args.promotion_metric}, threshold: {args.promotion_threshold:.2f}, direction: {args.promotion_direction}}}\n"
        f"\n"
        f"serving:\n"
        f"  model_id: {name_lower}\n"
        f"  aliases: [Production, Canary, Staging]\n"
        f"\n"
        f"prefect:\n"
        f'  schedule: "0 2 * * *"\n'
        f"  deployment_name: examlops-{name_lower}-nightly\n"
        f"  work_pool: default-agent\n"
        f"  concurrency_limit: 1\n"
        f"\n"
        f"inference:\n"
        f"  input_schema:\n"
        f"    embedding: list[float]\n"
        f"  output_schema:\n"
        f"    {output_key}: {output_type}\n"
    )

    if args.stdout_json:
        out: dict[str, str] = {
            str(config_file.relative_to(write_root)): config_content,
            str(yaml_file.relative_to(write_root)): yaml_content,
        }
        if not args.skip_model_class:
            out[str(model_file.relative_to(write_root))] = model_content
            out[str(init_file.relative_to(write_root))] = init_content
            out[str(test_file.relative_to(write_root))] = test_content
        print(json.dumps(out))
        return 0

    if args.skip_model_class:
        print(f"\nRegistering {name} ({args.task} / {args.task_type}) — pipeline files only")
    else:
        print(f"\nScaffolding {name} ({args.task} / {args.task_type})")
        _write(model_file, model_content, force=args.force, write_root=write_root)
        _write(init_file, init_content, force=args.force, write_root=write_root)

    _write(config_file, config_content, force=args.force, write_root=write_root)
    if not args.skip_model_class:
        _write(test_file, test_content, force=args.force, write_root=write_root)
    _write(yaml_file, yaml_content, force=args.force, write_root=write_root)

    if args.skip_model_class:
        print(
            f"\n{name} registered in pipeline. Next steps:\n"
            f"  1. Edit {yaml_file.relative_to(write_root)} — tune lifecycle thresholds & dataset config.\n"
            f"  2. Run: exa pipeline validate   # confirm {name} YAML is valid\n"
            f"  3. Run: exa pipeline run --model {name} --dataset FDataDataset --dummy\n"
        )
    else:
        print(
            f"\n{name} scaffolded. Next steps:\n"
            f"  1. Edit {model_file.relative_to(write_root)} — implement train_step / predict_step.\n"
            f"  2. Edit {yaml_file.relative_to(write_root)} — tune lifecycle thresholds.\n"
            f"  3. Run: exa pipeline validate   # confirm {name} YAML is valid\n"
            f"  4. Run: exa pipeline run --model {name} --dataset FDataDataset --dummy\n"
            f"  5. Run: pytest tests/unit/test_{name_lower}.py -v\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
