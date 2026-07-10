from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


def test_docs_markdown_covers_tree():
    result = runner.invoke(app, ["docs"])
    assert result.exit_code == 0, result.output
    assert "# `exa`" in result.output
    assert "`exa mcp serve`" in result.output  # nested subcommand present
    assert "`exa retrain`" in result.output


def test_docs_markdown_includes_options():
    # Regression: TyperOption subclasses click.Parameter (not click.Option), so an
    # isinstance(param, click.Option) filter silently dropped every flag from the reference.
    result = runner.invoke(app, ["docs"])
    assert result.exit_code == 0, result.output
    assert "`--output, -o`" in result.output  # root global option documented
    # Framework-auto meta options stay out of the reference.
    assert "--install-completion" not in result.output


def test_docs_json_tree():
    result = runner.invoke(app, ["--json", "docs"])
    assert result.exit_code == 0, result.output
    tree = json.loads(result.output)
    assert tree["name"] == "exa"
    names = {s["name"] for s in tree["subcommands"]}
    assert "exa mcp" in names
    # options captured on the root
    assert any(o["opts"].startswith("--output") for o in tree["options"])


def test_docs_writes_file(tmp_path):
    out = tmp_path / "cli.md"
    result = runner.invoke(app, ["docs", "--out", str(out)])
    assert result.exit_code == 0, result.output
    content = out.read_text()
    assert content.startswith("# `exa`")
    assert "`exa mcp resources`" in content
