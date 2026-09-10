"""`platform/ci/release_version.py` — one version for every artifact a tag publishes (ADR 0129).

The release workflow runs this script before it builds anything: ``tag`` refuses a tag that
disagrees with the tree, ``check`` refuses a tree whose copies disagree with each other, and
``notes`` produces the GitHub Release body. A bug in any of the three either blocks a correct
release or lets an inconsistent one out, so each is exercised against a scratch tree here —
never against the real files, which ``set`` would rewrite.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "platform" / "ci" / "release_version.py"

_spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
assert _spec and _spec.loader
rv = importlib.util.module_from_spec(_spec)
sys.modules["release_version"] = rv  # dataclass resolution needs the module registered
_spec.loader.exec_module(rv)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of just the files the script reads, so ``set`` can be run for real."""
    for field in (rv.SOURCE_FIELD, *rv.VERSION_FILES):
        dst = tmp_path / field.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / field.path, dst)
    shutil.copy(ROOT / rv.CHANGELOG, tmp_path / rv.CHANGELOG)
    return tmp_path


def test_the_real_tree_is_consistent():
    """Every copy agrees today; a drifted copy fails here before it fails a release."""
    assert rv.mismatches() == []


def test_every_copy_is_a_real_field():
    """A pattern that matches nothing would report MISSING forever and be ignored."""
    for field in rv.VERSION_FILES:
        assert field.read(ROOT), f"{field.path}: {field.label} pattern matches nothing"


def test_check_names_the_copy_that_drifted(tree: Path):
    chart = tree / "platform/infra/helm/examlops/Chart.yaml"
    chart.write_text(
        chart.read_text().replace(f'appVersion: "{rv.current(tree)}"', 'appVersion: "9.9.9"')
    )
    problems = rv.mismatches(tree)
    assert len(problems) == 1 and "appVersion" in problems[0] and "9.9.9" in problems[0]


def test_set_moves_every_copy_and_only_the_version(tree: Path):
    cli = tree / "platform/cli/pyproject.toml"
    before = cli.read_text()
    rv.set_version("7.8.9", tree)
    assert rv.current(tree) == "7.8.9"
    assert rv.mismatches(tree) == []
    after = cli.read_text()
    # Exactly one line changed: dependency floors and comments that hold versions are untouched.
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert changed == [(f'version = "{rv.current(ROOT)}"', 'version = "7.8.9"')]


def test_set_accepts_a_prerelease(tree: Path):
    rv.set_version("1.0.0-rc.1", tree)
    assert rv.mismatches(tree) == []


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "01.0.0", "1.0.0+build", "latest", ""])
def test_set_refuses_what_is_not_semver(tree: Path, bad: str):
    with pytest.raises(SystemExit):
        rv.set_version(bad, tree)
    assert rv.mismatches(tree) == []


def test_set_refuses_a_partial_bump(tree: Path):
    """A copy that lost its field must stop the bump before ANY file is written."""
    serving = tree / "serving/pyproject.toml"
    serving.write_text(serving.read_text().replace("version = ", "#version = ", 1))
    source_before = (tree / "pyproject.toml").read_text()
    with pytest.raises(SystemExit, match="nothing was changed"):
        rv.set_version("7.8.9", tree)
    assert (tree / "pyproject.toml").read_text() == source_before


def test_tag_must_name_the_tree_version(tree: Path):
    version = rv.current(tree)
    assert rv.check_tag(f"v{version}", tree) is None
    assert rv.check_tag(version, tree)  # no leading v: not a release tag
    assert rv.check_tag("v0.0.1", tree)


def test_notes_read_both_heading_styles():
    """The CHANGELOG writes both `## [0.46.0]` and `## [v0.48.0]`."""
    assert rv.release_notes("0.46.0").strip()
    assert rv.release_notes("0.48.0").strip()


def test_notes_collect_every_section_a_version_heads():
    """`0.37.0` heads two sections; the second is a whole release wave and must not vanish."""
    assert "Enterprise-readiness Phase 0 completion wave" in rv.release_notes("0.37.0")


def test_notes_stop_at_the_next_version():
    body = rv.release_notes(rv.current())
    assert "\n## [" not in body


def test_notes_treat_the_dots_as_literal(tmp_path: Path):
    """`0.51.0` must not match a `0x51y0` heading — a regex dot is a wildcard."""
    (tmp_path / "CHANGELOG.md").write_text("## [0x51y0]\nwrong\n## [0.51.0]\nright\n")
    assert rv.release_notes("0.51.0", tmp_path) == "right\n"
    assert rv.release_notes("0.5.0", tmp_path) == ""


def test_notes_agree_with_the_gitlab_extractor_for_every_tag():
    """Byte-for-byte parity with the awk program the published releases were built with.

    Only while `.gitlab-ci.yml` still carries it; the day that file goes, this parity has
    done its job and the tests above carry the semantics on their own.
    """
    ci = ROOT / ".gitlab-ci.yml"
    if not ci.exists() or shutil.which("awk") is None or shutil.which("git") is None:
        pytest.skip("the GitLab extractor is not available to compare against")
    from tests.unit.test_release_notes_are_extractable import _awk_program, _tags

    program = _awk_program()
    for tag in _tags():
        version = tag[1:]
        awk = subprocess.run(
            ["awk", "-v", f"v={version}", program, str(ROOT / "CHANGELOG.md")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert rv.release_notes(version).strip() == awk.strip(), f"notes differ for {tag}"


def test_cli_check_and_notes_exit_codes():
    ok = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    missing = subprocess.run(
        [sys.executable, str(SCRIPT), "notes", "0.0.0"], capture_output=True, text=True
    )
    assert missing.returncode == 1 and "no '## [0.0.0]' section" in missing.stderr
    bad_tag = subprocess.run(
        [sys.executable, str(SCRIPT), "tag", "v0.0.0"], capture_output=True, text=True
    )
    assert bad_tag.returncode == 1
