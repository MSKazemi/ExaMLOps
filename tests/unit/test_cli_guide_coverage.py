"""`docs/reference/cli-commands-guide.md` claims to document *every* `exa` command.

That claim is only true on the day someone last checked it. A new subcommand is one
decorator; the guide is a hand-written table; nothing connects them — so the guide decays
into a document that is confidently wrong about the tool it describes, which is worse than
one that admits it is partial. `exa eval operator-qa` had already slipped through.

The guide is deliberately hand-written — it carries a *use case* and a chosen example,
which no generator produces — so the fix is not to generate it. It is to fail the build
when a command exists that the guide has never heard of.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "reference" / "cli-commands-guide.md"

runner = CliRunner()


def _tree() -> dict:
    result = runner.invoke(app, ["--json", "docs"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _leaf_commands() -> list[str]:
    """Every leaf of the live Click tree, as the full invocation string."""
    leaves: list[str] = []

    def walk(node: dict) -> None:
        kids = node.get("subcommands") or []
        if not kids:
            leaves.append(node["name"])
            return
        for kid in kids:
            walk(kid)

    walk(_tree())
    return leaves


def _all_command_names() -> set[str]:
    """Leaves *and* groups.

    Not every group is only a namespace: `exa audit` carries its own ``--last`` /
    ``--model`` options and runs on its own, so the guide documents it as a command even
    though the tree walk classes it as a branch. For the "does this still exist" direction
    a group name is a real answer.
    """
    names: set[str] = set()

    def walk(node: dict) -> None:
        names.add(node["name"])
        for kid in node.get("subcommands") or []:
            walk(kid)

    walk(_tree())
    return names


def test_the_tree_is_actually_read():
    """A parser that silently returned nothing would make the coverage test vacuous."""
    leaves = _leaf_commands()
    assert len(leaves) > 300, len(leaves)
    assert "exa status" in leaves


def test_every_command_appears_in_the_guide():
    text = GUIDE.read_text()
    missing = sorted(c for c in _leaf_commands() if f"`{c}" not in text)
    assert not missing, (
        f"{len(missing)} command(s) exist but are absent from {GUIDE.relative_to(REPO)}, "
        f"which claims to document every one: {missing}. Add a row (what · use case · "
        "example) to the lifecycle panel where an operator would look for it."
    )


def test_the_guide_does_not_document_commands_that_no_longer_exist():
    """The other direction: a removed command leaves a row telling people to run it."""
    live = _all_command_names()
    documented = {
        line.split("`")[1].split(" ")[0] + " " + " ".join(line.split("`")[1].split(" ")[1:])
        for line in GUIDE.read_text().splitlines()
        if line.startswith("| `exa ")
    }
    # A row's example may carry arguments (`exa models diff <name> <a> <b>`); keep the
    # longest prefix that is itself a real command, and complain only when none is.
    stale = []
    for d in documented:
        parts = d.split()
        if not any(" ".join(parts[:n]) in live for n in range(len(parts), 1, -1)):
            stale.append(d)
    assert not stale, f"documented but not in the CLI: {sorted(stale)}"
