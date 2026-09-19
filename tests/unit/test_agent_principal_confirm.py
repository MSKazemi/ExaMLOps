# tests/unit/test_agent_principal_confirm.py
"""USAR I0 — `-o json` is an output format, not consent, for an agent (ADR 0147 d2).

`_output.confirm` returned True whenever structured output or `--yes` was on. That is right for a
human's script and wrong for an agent, whose "consent" would then be a flag it sets on itself.
An agent principal (`EXAMLOPS_PRINCIPAL_KIND=agent`) is now refused with `plan_required`; humans
behave exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import typer

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _output  # noqa: E402


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", "agent")


@pytest.mark.parametrize("flag", ["json_mode", "yes_mode"])
def test_a_human_is_still_auto_confirmed(monkeypatch, flag):
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    monkeypatch.setattr(_output, flag, True)
    assert _output.principal_kind() == "human"
    assert _output.confirm("Apply?") is True


@pytest.mark.parametrize("flag", ["json_mode", "yes_mode", None])
def test_an_agent_is_never_auto_confirmed(agent, monkeypatch, capsys, flag):
    if flag:
        monkeypatch.setattr(_output, flag, True)
    with pytest.raises(typer.Exit):
        _output.confirm("Apply traffic split?")
    captured = capsys.readouterr()
    assert "plan_required" in captured.out + captured.err


def test_only_the_exact_value_marks_an_agent(monkeypatch):
    for value in ("robot", "", "human", "agents"):
        monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", value)
        assert _output.principal_kind() == "human"
    monkeypatch.setenv("EXAMLOPS_PRINCIPAL_KIND", " Agent ")
    assert _output.principal_kind() == "agent"


def test_a_real_mutation_is_refused_before_it_writes(agent, tmp_path, monkeypatch):
    """End to end: `serve traffic --yes -o json` as an agent changes nothing."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    from examlops.cli.commands import serve
    from examlops.platform_db import get_traffic_rules, init_db

    init_db()
    monkeypatch.setattr(_output, "json_mode", True)
    monkeypatch.setattr(_output, "output_format", "json")
    monkeypatch.setattr(_output, "yes_mode", True)
    result = CliRunner().invoke(
        serve.app, ["traffic", "JPCP", "--production", "90", "--canary", "10"]
    )
    assert result.exit_code != 0
    assert "plan_required" in result.output
    assert get_traffic_rules("JPCP") is None


# ── ADR 0147 d2 finding: a command's own --yes/--force bypassed confirm() (and the agent
#    check embedded in it) entirely, for commands that guarded the *call* rather than routing
#    the local flag through it. Fixed at every real site found; guarded here so it cannot
#    silently reopen. ─────────────────────────────────────────────────────────────────────


def test_no_command_guards_the_confirm_call_itself(monkeypatch):
    """Static guard: nothing may skip calling `_output.confirm(...)` via a local flag.

    A command's own bypass flag (its own `--yes`/`--force`, or one of the global
    `yes_mode`/`json_mode`) must be passed *into* `confirm(..., auto_yes=flag)` so the agent
    check inside it always runs — never used to decide whether to call it at all. `dry_run` is
    the one legitimate exemption: it is threaded into the mutating function too, so the call it
    guards is a genuine no-op for everyone, agent included.
    """
    import re
    from pathlib import Path

    cli_root = Path(__file__).parents[2] / "platform" / "cli" / "src" / "examlops" / "cli"
    pattern = re.compile(r"if not (\w+(?:\.\w+)?) and not _output\.confirm\(")
    offenders = []
    for path in cli_root.rglob("*.py"):
        text = path.read_text()
        for match in pattern.finditer(text):
            guard = match.group(1)
            if guard != "dry_run":
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(cli_root)}:{line} (guarded by {guard!r})")
    assert offenders == [], (
        "confirm() call(s) guarded by a bypassable flag instead of auto_yes= — "
        f"an agent principal could skip the plan_required check: {offenders}"
    )


@pytest.mark.parametrize(
    ("module_name", "app_attr", "argv", "setup"),
    [
        ("examlops.cli.commands.connection_cmd", "app", ["delete", "c1", "--yes"], "connection"),
        (
            "examlops.cli.commands.workbench_cmd",
            "app",
            ["delete", "wb1", "-p", "p1", "--yes"],
            None,
        ),
        ("examlops.cli.commands.project_cmd", "app", ["archive", "p1", "--yes"], "project"),
    ],
)
def test_a_commands_own_yes_no_longer_bypasses_the_agent_gate(
    agent, tmp_path, monkeypatch, module_name, app_attr, argv, setup
):
    """A command's *local* `--yes` used to skip `confirm()` (and the agent check) entirely."""
    import importlib

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    from examlops.platform_db import init_db

    init_db()
    if setup == "connection":
        from examlops.connections import create_connection

        create_connection("c1", "uri", config={"uri": "s3://x"})
    elif setup == "project":
        from examlops.data.projects import create_project

        create_project("p1")

    module = importlib.import_module(module_name)
    result = CliRunner().invoke(getattr(module, app_attr), argv)
    assert result.exit_code != 0
    assert "plan_required" in result.output


def test_a_human_with_the_commands_own_yes_is_unaffected(tmp_path, monkeypatch):
    """Regression guard: the fix must not change human behaviour — local --yes still means yes."""
    monkeypatch.delenv("EXAMLOPS_PRINCIPAL_KIND", raising=False)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    from examlops.cli.commands import connection_cmd
    from examlops.connections import create_connection, get_connection
    from examlops.platform_db import init_db

    init_db()
    create_connection("c1", "uri", config={"uri": "s3://x"})
    result = CliRunner().invoke(connection_cmd.app, ["delete", "c1", "--yes"])
    assert result.exit_code == 0, result.output
    assert get_connection("c1") is None
