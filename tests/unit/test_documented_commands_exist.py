"""Every `exa …` command the documentation names is a command the CLI has.

The docs carry **2609** `exa …` invocations outside the runbooks. They are the platform's main
interface for most readers: a guide that names a command which has since been renamed does not
merely mislead, it makes the reader doubt the rest of the page. Three had drifted when this guard
was written — `exa challenger` (really `exa serve challenger judge`), `exa synth` (`exa data
synth`) and `exa infer predict` (`exa predict`) — each a plausible-looking invocation that simply
does not exist.

This subsumes the runbook-specific check: the scan covers all of `docs/`, runbooks included.
`tests/unit/test_alert_runbooks.py` keeps the *link* checks (alert → url → section → alert), which
are about structure rather than content.

**The instrument is asserted before its output is believed.** Three wrong versions of this scan each
reported every documented command as broken, which reads exactly like catastrophic drift; the cause
was that the tree's `name` is the full path (`exa approvals list`), not a leaf to be joined with its
parents. A scan whose failure mode looks like a catastrophe has to prove it can see first.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

from examlops.cli.main import app  # noqa: E402

DOCS = ROOT / "docs"

#: Documented invocations that are deliberately not real commands, with the reason.
ALLOWED: dict[str, str] = {}


def _live_commands() -> set[str]:
    result = CliRunner().invoke(app, ["--json", "docs"])
    assert result.exit_code == 0, result.output
    commands: set[str] = set()

    def walk(node: dict) -> None:
        if node.get("name"):
            commands.add(node["name"].strip())  # already the full path
        for kid in node.get("subcommands") or []:
            walk(kid)

    walk(json.loads(result.stdout))
    return commands


def test_the_scan_can_see_before_anything_is_concluded_from_it():
    commands = _live_commands()
    assert len(commands) > 200, f"only {len(commands)} commands parsed — the tree's shape changed"
    assert {"exa approvals list", "exa audit verify", "exa data synth"} <= commands
    assert "exa nonsense zzz" not in commands

    pages = list(DOCS.rglob("*.md"))
    assert len(pages) > 50, f"only {len(pages)} pages found — the docs path is stale"


def test_every_documented_exa_command_exists():
    commands = _live_commands()
    mentions = 0
    missing: list[str] = []
    for page in sorted(DOCS.rglob("*.md")):
        for match in re.finditer(r"`(exa [a-z0-9][a-z0-9 _-]*)", page.read_text(encoding="utf-8")):
            invocation = " ".join(match.group(1).split())
            mentions += 1
            if invocation in ALLOWED:
                continue
            parts = invocation.split()
            # The longest prefix that is a real command; flags and arguments are not in the tree.
            if not any(" ".join(parts[:n]) in commands for n in range(len(parts), 1, -1)):
                missing.append(f"{page.relative_to(ROOT)}: `{invocation}`")
    assert mentions > 2000, f"only {mentions} `exa …` mentions found — the extraction is stale"
    assert not missing, (
        "the documentation names commands the CLI does not have, so a reader following one gets "
        "`No such command`:\n  " + "\n  ".join(sorted(set(missing)))
    )


def test_the_allow_list_names_only_things_still_documented():
    """An exemption that no page uses any more is a reason nobody can check."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in DOCS.rglob("*.md"))
    stale = sorted(inv for inv in ALLOWED if f"`{inv}" not in text)
    assert not stale, f"ALLOWED names invocations no page mentions: {stale}"
