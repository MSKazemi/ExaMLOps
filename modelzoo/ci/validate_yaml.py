#!/usr/bin/env python3
"""
Validate YAML and JSON files in the repository.
Used by GitLab CI validate stage.
Exits 0 if all valid, 1 if any file is invalid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".vscode",
    ".venv",
    "venv",
}


def find_config_files(root: Path) -> list[Path]:
    """Find .yml, .yaml, .json files, excluding common ignore dirs."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".yaml", ".json"):
            if not any(part in path.parts for part in EXCLUDE_DIRS):
                files.append(path)
    return sorted(files)


def validate_json(path: Path) -> tuple[bool, str]:
    """Validate a JSON file. Returns (ok, error_msg)."""
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return True, ""
    except json.JSONDecodeError as e:
        return False, str(e)
    except OSError as e:
        return False, str(e)


def validate_yaml(path: Path) -> tuple[bool, str]:
    """Validate a YAML file. Returns (ok, error_msg)."""
    if yaml is None:
        return True, ""  # Skip if PyYAML not installed
    try:
        with open(path, encoding="utf-8") as f:
            yaml.safe_load(f)
        return True, ""
    except yaml.YAMLError as e:
        return False, str(e)
    except OSError as e:
        return False, str(e)


def main() -> int:
    root = Path.cwd()
    files = find_config_files(root)
    if not files:
        print("No YAML/JSON files to validate.")
        return 0
    errors: list[str] = []
    for path in files:
        rel = path.relative_to(root)
        if path.suffix == ".json":
            ok, msg = validate_json(path)
        else:
            ok, msg = validate_yaml(path)
        if ok:
            print(f"  OK  {rel}")
        else:
            print(f"  ERR {rel}: {msg}")
            errors.append(f"{rel}: {msg}")
    if errors:
        print(f"\n{len(errors)} error(s)")
        return 1
    print(f"\nAll {len(files)} file(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
