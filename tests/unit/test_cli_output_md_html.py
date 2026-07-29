"""N3 — Markdown / HTML output formats (`--output md|html`).

Extends the central `_output` format dispatch (N4 added yaml/csv) so every structured
command can export a Markdown table (for reports/PRs) or an HTML table (for embedding),
not just `status`/`doctor`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_format():
    from examlops.cli import _output

    yield
    _output.output_format = "table"
    _output.json_mode = False


# --------------------------------------------------------------------- markdown


def test_md_list_of_dicts_is_a_table():
    from examlops.cli._output import _to_md

    out = _to_md([{"model": "JPCP", "stage": "prod"}, {"model": "FData", "stage": "staging"}])
    lines = out.splitlines()
    assert lines[0] == "| model | stage |"
    assert lines[1] == "| --- | --- |"
    assert "| JPCP | prod |" in lines
    assert "| FData | staging |" in lines


def test_md_dict_is_key_value_table():
    from examlops.cli._output import _to_md

    out = _to_md({"cpu": 4, "mem": "8G"})
    assert out.splitlines()[0] == "| Key | Value |"
    assert "| cpu | 4 |" in out


def test_md_escapes_pipes_and_flattens_newlines():
    from examlops.cli._output import _to_md

    out = _to_md({"note": "a|b\nc"})
    assert "a\\|b c" in out  # pipe escaped, newline flattened to a space


def test_md_non_tabular_degrades_to_json_block():
    from examlops.cli._output import _to_md

    out = _to_md(["x", "y"])
    assert out.startswith("```json")
    assert out.rstrip().endswith("```")


# ------------------------------------------------------------------------- html


def test_html_list_is_table_with_head_and_body():
    from examlops.cli._output import _to_html

    out = _to_html([{"m": "JPCP", "v": 1}])
    assert "<table>" in out and "</table>" in out
    assert "<thead><tr><th>m</th><th>v</th></tr></thead>" in out
    assert "<td>JPCP</td>" in out


def test_html_escapes_cell_values():
    from examlops.cli._output import _to_html

    out = _to_html([{"x": "<script>&"}])
    assert "&lt;script&gt;&amp;" in out
    assert "<script>" not in out  # never emit raw markup from data


def test_html_dict_and_non_tabular():
    from examlops.cli._output import _to_html

    assert "<td>4</td>" in _to_html({"cpu": 4})
    assert _to_html(["a", "b"]).startswith("<pre>")


# ------------------------------------------------------------- dispatch + enum


def test_print_json_dispatches_on_format(capsys):
    from examlops.cli import _output

    _output.output_format = "md"
    _output.print_json({"k": "v"})
    assert "| Key | Value |" in capsys.readouterr().out

    _output.output_format = "html"
    _output.print_json([{"a": 1}])
    assert "<table>" in capsys.readouterr().out


def test_output_format_enum_has_md_and_html():
    from examlops.cli.main import OutputFormat

    values = {f.value for f in OutputFormat}
    assert {"md", "html"} <= values


def test_cli_output_md_end_to_end():
    from examlops.cli.main import app

    res = runner.invoke(app, ["-o", "md", "config", "contexts"])
    assert res.exit_code == 0, res.output
    assert "| Key | Value |" in res.output


def test_cli_output_html_end_to_end():
    from examlops.cli.main import app

    res = runner.invoke(app, ["-o", "html", "config", "contexts"])
    assert res.exit_code == 0, res.output
    assert "<table>" in res.output
