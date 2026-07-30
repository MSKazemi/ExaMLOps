"""Guard: every `exa` command group shows a friendly epilog ("Learn more" footer, plus a
"Common tasks" block for curated groups).

Running a bare group (``exa serve``) should hand-hold like a leaf command does — see
``examlops.cli._group_help``. These tests fail loudly if a group ends up without the footer
(e.g. a newly added group, or a regression in the central ``attach_group_epilogs`` pass).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli._group_help import _COMMON_TASKS, _GUIDE  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


def _all_group_paths(node, prefix=()):
    """Yield (path_tuple, info) for every registered group, recursively."""
    for info in getattr(node, "registered_groups", []):
        name = getattr(info, "name", None)
        if not name:
            continue
        path = (*prefix, name)
        yield path, info
        sub = getattr(info, "typer_instance", None)
        if sub is not None:
            yield from _all_group_paths(sub, path)


def test_every_group_has_learn_more_footer():
    # Importing examlops.cli.main runs attach_group_epilogs; every group must carry the
    # generic footer with the docs-guide pointer.
    missing = [
        " ".join(path)
        for path, info in _all_group_paths(app)
        if not getattr(info, "epilog", None) or _GUIDE not in info.epilog
    ]
    assert not missing, f"groups missing the 'Learn more' footer: {missing}"


def test_curated_groups_show_common_tasks_block():
    # A representative curated group renders its examples in the rendered help.
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0, result.output
    assert "Common tasks" in result.output
    assert "Learn more" in result.output
    assert "exa explain serve" in result.output


def test_nested_group_epilog_is_path_scoped():
    # Nested sub-groups get a path-scoped footer (e.g. 'exa explain serve shadow').
    result = runner.invoke(app, ["serve", "shadow", "--help"])
    assert result.exit_code == 0, result.output
    assert "exa explain serve shadow" in result.output


def test_common_tasks_spec_examples_start_with_exa():
    # Cheap sanity guard on the curated spec: every example is an `exa …` invocation.
    for path, tasks in _COMMON_TASKS.items():
        for what, cmd in tasks:
            assert cmd.startswith("exa "), f"{path}: example not an exa command: {cmd!r}"
            assert what, f"{path}: empty description for {cmd!r}"
