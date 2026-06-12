from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import typer

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli import _output
from examlops.cli.commands import features_cmd

# Build a minimal test app with the features group registered
_test_app = typer.Typer(no_args_is_help=True)


@_test_app.callback()
def _cb(
    json: bool = typer.Option(False, "--json"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    _output.json_mode = json
    _output.yes_mode = yes


_test_app.add_typer(features_cmd.app, name="features", help="Feature store — versioned training features")

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_path = str(tmp_path / ".feature_store")
    os.environ["PLATFORM_DB"] = db_path
    os.environ["FEATURE_STORE_DIR"] = store_path
    from examlops.platform_db import init_db
    init_db()
    yield tmp_path
    os.environ.pop("PLATFORM_DB", None)
    os.environ.pop("FEATURE_STORE_DIR", None)


def _make_feature_file(tmp_path: Path, name: str = "feats.csv", content: str = "col1,col2\n1,2\n") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def test_features_list_empty():
    result = runner.invoke(_test_app, ["features", "list"])
    assert result.exit_code == 0, result.output
    assert "No feature versions found" in result.output


def test_features_push_creates_version(isolated_db):
    f = _make_feature_file(isolated_db)
    result = runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    assert result.exit_code == 0, result.output
    assert "Pushed JPCP/default v1" in result.output
    # Verify the file was copied into the store
    store = Path(os.environ["FEATURE_STORE_DIR"])
    stored_files = list((store / "JPCP" / "default").glob("v1_*.csv"))
    assert len(stored_files) == 1


def test_features_list_shows_version(isolated_db):
    f = _make_feature_file(isolated_db)
    runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    result = runner.invoke(_test_app, ["features", "list"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "default" in result.output
    assert "1" in result.output


def test_features_push_increments_version(isolated_db):
    f = _make_feature_file(isolated_db)
    result1 = runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    assert result1.exit_code == 0, result1.output
    assert "v1" in result1.output

    result2 = runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    assert result2.exit_code == 0, result2.output
    assert "v2" in result2.output

    # Both versions should appear in list
    result_list = runner.invoke(_test_app, ["features", "list", "JPCP"])
    assert result_list.exit_code == 0, result_list.output
    assert "2" in result_list.output


def test_features_pull_shows_info(isolated_db):
    f = _make_feature_file(isolated_db)
    runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    result = runner.invoke(_test_app, ["features", "pull", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "default" in result.output


def test_features_pull_copies_file(isolated_db):
    f = _make_feature_file(isolated_db)
    runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    out_path = str(isolated_db / "pulled.csv")
    result = runner.invoke(_test_app, ["features", "pull", "JPCP", "--output", out_path])
    assert result.exit_code == 0, result.output
    assert "Copied to" in result.output
    assert Path(out_path).exists()
    assert Path(out_path).read_text() == f.read_text()


def test_features_pull_not_found():
    result = runner.invoke(_test_app, ["features", "pull", "NONEXISTENT"])
    assert result.exit_code == 1
    assert "No feature version found" in result.output


def test_features_push_missing_file():
    result = runner.invoke(_test_app, ["features", "push", "JPCP", "/does/not/exist.csv"])
    assert result.exit_code == 1
    assert "File not found" in result.output


def test_features_list_filter_by_model(isolated_db):
    f = _make_feature_file(isolated_db)
    runner.invoke(_test_app, ["features", "push", "JPCP", str(f)])
    runner.invoke(_test_app, ["features", "push", "MACK", str(f)])
    result = runner.invoke(_test_app, ["features", "list", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "MACK" not in result.output
