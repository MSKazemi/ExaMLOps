"""Guard: every ``exa`` command Skipper's system prompt teaches must actually exist.

Why this test exists
--------------------
The prompt tells the agent to name the exact ``exa`` command an operator would type
(the "Name the command" hard rule). That instruction is only useful while the commands
it names are real: a prompt that teaches ``exa reproduce --verify`` when the product
ships ``exa reproduce verify`` sends operators to a command that exits 2, and nothing
else in the repo would notice — the prompt is a string, and no CLI test reads it.

This is the same failure the design record hit at scale (44 ADRs and 76 specs naming
pre-implementation shapes that the product later improved on). A string that names a
command is a claim about the product, so it gets a guard.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skipper.prompts import SYSTEM_PROMPT  # noqa: E402

# `exa ...` inside a backtick span, up to the first placeholder / flag / end.
_INVOCATION = re.compile(r"`(exa [a-z][^`]*)`")
_STOP = re.compile(r"^(<|--|\.\.\.)")


def _root_group():
    typer_main = pytest.importorskip("typer.main")
    pytest.importorskip("examlops.cli.main")
    from examlops.cli.main import app

    return typer_main.get_command(app)


def _split(invocation: str) -> tuple[list[str], list[str]]:
    """`exa a b --flag <X>` -> (["a", "b"], ["--flag"])."""
    words = invocation.split()[1:]  # drop "exa"
    path: list[str] = []
    flags: list[str] = []
    for word in words:
        if word.startswith("--"):
            flags.append(word.split("=")[0].rstrip(",.·"))
        elif not flags and not _STOP.match(word):
            path.append(word)
    return path, flags


def _resolve(root, path: list[str]):
    cmd = root
    for name in path:
        subs = getattr(cmd, "commands", None)
        if not subs or name not in subs:
            return None
        cmd = subs[name]
    return cmd


def _option_names(cmd) -> set[str]:
    import click

    ctx = click.Context(cmd)
    names: set[str] = set()
    for param in cmd.get_params(ctx):
        if getattr(param, "param_type_name", None) == "option":
            names.update(param.opts)
    return names


def _matches_declared_pattern(cmd, flag: str) -> bool:
    """True if ``flag`` instantiates a pattern the command publishes for `exa docs`.

    A command that parses its own flags out of ``ctx.args`` declares none of them to Click
    — `exa pipeline promote` accepts ``--if-<metric>-<op>`` where the metric is whatever the
    model logged. Such a command publishes the shape via ``dynamic_options``; matching
    against that is stronger evidence than finding the flag mentioned in the help text,
    because prose can outlive the flag it describes.
    """
    import re as _re

    for dyn in getattr(cmd.callback, "dynamic_options", None) or []:
        pattern = str(dyn.get("opts", "")).split(",")[0].strip().split(" ")[0]
        if "<" not in pattern:
            continue
        parts = _re.split(r"<[^>]*>", pattern)
        rx = "^" + "[^\\s-]+".join(_re.escape(part) for part in parts) + "$"
        if _re.match(rx, flag):
            return True
    return False


def _invocations() -> list[str]:
    return sorted({m.group(1) for m in _INVOCATION.finditer(SYSTEM_PROMPT)})


def test_prompt_carries_the_name_the_command_rule():
    """The rule Mohsen decided on 2026-08-28 — deleting it silently regresses the eval."""
    assert "Name the command" in SYSTEM_PROMPT
    assert "exa" in SYSTEM_PROMPT
    # It must be stated as an obligation, not a suggestion.
    assert "MUST also name the exact" in SYSTEM_PROMPT


def test_prompt_names_at_least_the_known_operator_intents():
    """A regression here means the mapping table was gutted, not merely reworded."""
    assert len(_invocations()) >= 10


@pytest.mark.parametrize("invocation", _invocations())
def test_every_exa_command_in_the_prompt_exists(invocation: str):
    root = _root_group()
    path, flags = _split(invocation)
    assert path, f"could not parse a command path out of {invocation!r}"

    cmd = _resolve(root, path)
    assert cmd is not None, (
        f"the system prompt teaches `{invocation}` but `exa {' '.join(path)}` "
        "does not resolve in the CLI tree"
    )

    if flags:
        # A flag counts as real if it is a declared Click option OR the command's own
        # help documents it. The second arm is not laziness: `exa pipeline promote`
        # parses `--if-<metric>-<op> <value>` in its body rather than declaring each
        # one, so an options-only check calls the product's real, documented flag a
        # defect. What this still catches is an invented flag, which is the risk.
        available = _option_names(cmd)
        documented = f"{cmd.help or ''}\n{getattr(cmd, 'epilog', '') or ''}"
        missing = [
            f
            for f in flags
            if f not in available and not _matches_declared_pattern(cmd, f) and f not in documented
        ]
        assert not missing, (
            f"the system prompt teaches `{invocation}` but `exa {' '.join(path)}` "
            f"neither declares nor documents {missing} (declared: {sorted(available)})"
        )
