"""Guard: every `exa` command group shows a friendly epilog ("Learn more" footer, plus a
"Common tasks" block for curated groups).

Running a bare group (``exa serve``) should hand-hold like a leaf command does — see
``examlops.cli._group_help``. These tests fail loudly if a group ends up without the footer
(e.g. a newly added group, or a regression in the central ``attach_group_epilogs`` pass).
"""

from __future__ import annotations

import json
import re
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


def _live_commands() -> tuple[set[str], set[str]]:
    """(every node name, the subset that are groups).

    Groups are included as names in their own right — `exa audit --last 7d` targets one —
    but knowing *which* names are groups is what lets the check below catch a renamed leaf
    instead of quietly matching its still-existing parent.
    """
    result = runner.invoke(app, ["--json", "docs"])
    assert result.exit_code == 0, result.output
    names: set[str] = set()
    groups: set[str] = set()

    def walk(node: dict) -> None:
        names.add(node["name"])
        kids = node.get("subcommands") or []
        if kids:
            groups.add(node["name"])
        for kid in kids:
            walk(kid)

    walk(json.loads(result.stdout))
    return names, groups


def _resolves(cmd: str, live: set[str], groups: set[str]) -> bool:
    """Does this example name a command that exists?

    Three shapes have to survive: arguments after the command (`exa data diff FData
    <revA>`), a *global* option before it (`exa --json agent status`, `exa -c lxp agent
    status`), and a group used directly (`exa audit --last 7d`). So: skip the leading option
    run — including an option's value, unless that value is itself a real subcommand name —
    then take the longest remaining prefix that is a command.

    Longest-prefix alone is too generous: a renamed leaf (`exa eval calibration ls`) still
    matches its surviving parent group and passes. So when the match lands on a *group* and
    the next token looks like a subcommand name — lowercase, not an option, not a `<placeholder>`
    or an obvious value — that token has to be a real child.
    """
    toks = cmd.split()[1:]  # everything after `exa`
    i = 0
    while i < len(toks) and toks[i].startswith("-"):
        i += 1
        if i < len(toks) and not toks[i].startswith("-") and f"exa {toks[i]}" not in live:
            i += 1  # that was the option's value, not the subcommand
    rest = toks[i:]
    for n in range(len(rest), 0, -1):
        prefix = "exa " + " ".join(rest[:n])
        if prefix not in live:
            continue
        nxt = rest[n] if n < len(rest) else None
        if prefix in groups and nxt and re.fullmatch(r"[a-z][a-z0-9-]*", nxt):
            return f"{prefix} {nxt}" in live
        return True
    return False


def test_every_curated_example_names_a_command_that_exists():
    """An epilog example is copy-paste UX: a stale one is worse than no example at all.

    `--help` is where someone goes when they are already unsure, and an example that errors
    teaches them the tool is broken rather than that the line is old. Nothing connected the
    curated spec to the command tree, so a rename would have left the wrong line rendering
    happily in `exa <group> --help`.
    """
    live, groups = _live_commands()
    stale = [
        (path, cmd)
        for path, tasks in _COMMON_TASKS.items()
        for _, cmd in tasks
        if not _resolves(cmd, live, groups)
    ]
    assert not stale, (
        f"{len(stale)} curated example(s) name a command that does not exist: {stale}. "
        "Update the example in examlops.cli._group_help._COMMON_TASKS."
    )


def test_every_spec_key_is_a_real_group():
    """A key that matches no group is silently dead — its examples render nowhere."""
    live, _ = _live_commands()
    orphans = sorted(k for k in _COMMON_TASKS if f"exa {k}" not in live)
    assert not orphans, f"_COMMON_TASKS keys that are not live groups: {orphans}"
