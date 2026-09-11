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
