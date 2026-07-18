"""Version-consistency CI gate (enterprise-readiness Phase 0, item 0.10).

The platform version is stated in four places that drifted apart (root ``pyproject`` 0.33.0
vs cli ``pyproject`` 0.36.0 vs CHANGELOG 0.35.0 vs git tag v0.36.0). This gate fails the build
whenever they disagree, so a release can never ship an inconsistent version again.

Rules enforced:
  * root ``pyproject.toml`` version  ==  cli ``pyproject.toml`` version  ==  latest released
    CHANGELOG section (the internal surfaces must ALWAYS agree);
  * that version is a valid semver and is ``>=`` the latest ``vX.Y.Z`` git tag (monotonic —
    equal at release time, ahead while unreleased work accumulates).

The git-tag check is skipped gracefully when git or tags are unavailable (e.g. a shallow CI
checkout or a source tarball), so the internal-consistency guarantee still runs everywhere.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _semver_tuple(v: str) -> tuple[int, int, int]:
    m = _SEMVER.match(v.strip().lstrip("v"))
    assert m, f"not a semver: {v!r}"
    return int(m[1]), int(m[2]), int(m[3])


def _pyproject_version(path: Path) -> str:
    data = tomllib.loads(path.read_text())
    # Support both [project] and [tool.poetry] layouts.
    proj = data.get("project", {})
    if "version" in proj:
        return proj["version"]
    return data["tool"]["poetry"]["version"]


def _changelog_latest_release() -> str:
    """First ``## [X.Y.Z]`` heading in CHANGELOG.md, skipping ``## [Unreleased]``."""
    text = (_ROOT / "CHANGELOG.md").read_text()
    for m in re.finditer(r"^##\s*\[([^\]]+)\]", text, re.MULTILINE):
        label = m.group(1).strip()
        if label.lower() == "unreleased":
            continue
        return label
    raise AssertionError("no released version section found in CHANGELOG.md")


def _latest_git_tag() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), "tag", "--list", "v*.*.*"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    tags = [t for t in out.stdout.split() if _SEMVER.match(t.lstrip("v"))]
    if not tags:
        return None
    return max(tags, key=lambda t: _semver_tuple(t))


def test_internal_surfaces_agree():
    root_v = _pyproject_version(_ROOT / "pyproject.toml")
    cli_v = _pyproject_version(_ROOT / "platform" / "cli" / "pyproject.toml")
    changelog_v = _changelog_latest_release()
    assert root_v == cli_v == changelog_v, (
        f"version drift: root pyproject={root_v!r}, cli pyproject={cli_v!r}, "
        f"CHANGELOG={changelog_v!r} — these must always match"
    )
    _semver_tuple(root_v)  # must be valid semver


def test_version_is_monotonic_vs_latest_tag():
    tag = _latest_git_tag()
    if tag is None:
        pytest.skip("no git tags available (shallow checkout / source tarball)")
    current = _pyproject_version(_ROOT / "pyproject.toml")
    assert _semver_tuple(current) >= _semver_tuple(tag), (
        f"current version {current!r} is behind the latest release tag {tag!r} — "
        "bump pyproject + CHANGELOG before/at release"
    )
