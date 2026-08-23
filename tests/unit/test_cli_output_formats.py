from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _output  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


# ── _to_csv / _to_yaml units ──────────────────────────────────────────────────


def test_to_csv_list_of_dicts():
    out = _output._to_csv([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["a", "b"]
    assert rows[1] == ["1", "2"]
    assert rows[2] == ["3", "4"]


def test_to_csv_flattens_nested_cells():
    out = _output._to_csv([{"name": "x", "aliases": {"Production": "5"}}])
    rows = list(csv.reader(io.StringIO(out)))
    # nested dict is JSON-encoded into a single cell
    assert json.loads(rows[1][1]) == {"Production": "5"}


def test_to_csv_dict_is_key_value():
    out = _output._to_csv({"name": "ExaMLOps", "version": "1.0"})
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["key", "value"]
    assert ["name", "ExaMLOps"] in rows


def test_to_csv_non_tabular_falls_back_to_json():
    out = _output._to_csv(42)
    assert json.loads(out) == 42


def test_to_yaml_roundtrips():
    import yaml

    out = _output._to_yaml({"name": "ExaMLOps", "skills": ["a", "b"]})
    assert yaml.safe_load(out) == {"name": "ExaMLOps", "skills": ["a", "b"]}


# ── CLI --output wiring ───────────────────────────────────────────────────────


def test_output_csv_renders_csv_table():
    result = runner.invoke(app, ["-o", "csv", "mcp", "tools"])
    assert result.exit_code == 0, result.output
    header = result.output.strip().splitlines()[0]
    assert header.startswith("Tool,Kind,Tags,Description")


def test_output_yaml_renders_yaml():
    import yaml

    result = runner.invoke(app, ["-o", "yaml", "mcp", "agent-card"])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(result.output)
    assert doc["name"] == "ExaMLOps"


def test_output_json_equivalent_to_json_flag():
    a = runner.invoke(app, ["-o", "json", "mcp", "agent-card"])
    b = runner.invoke(app, ["--json", "mcp", "agent-card"])
    assert a.exit_code == 0 and b.exit_code == 0
    assert json.loads(a.output)["name"] == json.loads(b.output)["name"] == "ExaMLOps"


def test_default_is_human_table():
    result = runner.invoke(app, ["mcp", "tools"])
    assert result.exit_code == 0, result.output
    assert "┏" in result.output  # Rich table border → human mode


def test_format_resets_between_invocations():
    # A structured invocation must not leak into a later default (table) invocation.
    runner.invoke(app, ["-o", "json", "mcp", "agent-card"])
    result = runner.invoke(app, ["mcp", "tools"])
    assert "┏" in result.output
    assert _output.output_format == "table"


def test_invalid_output_format_rejected():
    result = runner.invoke(app, ["-o", "xml", "mcp", "tools"])
    assert result.exit_code != 0


# ── Rich markup in plain prose ────────────────────────────────────────────────
# Callers pass prose, and prose contains brackets: `examlops[chat]`, a TOML
# `[project.entry-points]` header, a `[WARNING]` log line. Rich reads a bracketed word as a
# style tag and *silently deletes it* — so `pip install examlops[chat]` reached the terminal
# as `pip install examlops`, an instruction that installs the wrong thing without erroring.
# `hint()` was escaped for exactly this reason; the rest of the family was not.


@pytest.mark.parametrize(
    "fn,capture",
    [
        ("ok", "out"),
        ("warning", "err"),
        ("info", "out"),
        ("hint", "out"),
    ],
)
def test_bracketed_text_survives_to_the_terminal(fn, capture, capsys, monkeypatch):
    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", False)
    monkeypatch.setattr(_output, "quiet_mode", False)
    getattr(_output, fn)("run: pip install examlops[chat]")
    captured = capsys.readouterr()
    assert "examlops[chat]" in (captured.out if capture == "out" else captured.err)


def test_an_error_and_its_hint_both_survive(capsys, monkeypatch):
    import typer

    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", False)
    with pytest.raises(typer.Exit):
        _output.error("psycopg missing [pgvector]", hint="pip install examlops[vector]")
    err = capsys.readouterr().err
    assert "[pgvector]" in err
    assert "examlops[vector]" in err


def test_detail_too(capsys, monkeypatch):
    from examlops.cli import _output

    monkeypatch.setattr(_output, "json_mode", False)
    monkeypatch.setattr(_output, "quiet_mode", False)
    monkeypatch.setattr(_output, "verbose_mode", True)
    _output.detail("resolved [chat] extra")
    assert "[chat]" in capsys.readouterr().out
