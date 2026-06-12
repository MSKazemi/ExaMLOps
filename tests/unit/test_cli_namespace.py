from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    from examlops.platform_db import init_db

    init_db()
    yield
    os.environ.pop("PLATFORM_DB", None)


# --- 1. namespace list shows "default" auto-created ---

def test_namespace_list_autocreates_default():
    result = runner.invoke(app, ["namespace", "list"])
    assert result.exit_code == 0, result.output
    assert "default" in result.output


# --- 2. namespace create + list shows two namespaces ---

def test_namespace_create_and_list():
    result = runner.invoke(app, ["namespace", "create", "myproject"])
    assert result.exit_code == 0, result.output
    assert "myproject" in result.output

    result = runner.invoke(app, ["namespace", "list"])
    assert result.exit_code == 0, result.output
    assert "default" in result.output
    assert "myproject" in result.output


# --- 3. namespace create duplicate -> error "already exists", exit 1 ---

def test_namespace_create_duplicate_errors():
    runner.invoke(app, ["namespace", "create", "myproject"])
    result = runner.invoke(app, ["namespace", "create", "myproject"])
    assert result.exit_code == 1
    assert "already exists" in result.output


# --- 4. namespace assign records the model ---

def test_namespace_assign_records():
    runner.invoke(app, ["namespace", "create", "myproject"])
    result = runner.invoke(app, ["namespace", "assign", "JPCP", "--namespace", "myproject"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "myproject" in result.output


# --- 5. namespace info shows assigned model ---

def test_namespace_info_shows_model():
    runner.invoke(app, ["namespace", "create", "myproject"])
    runner.invoke(app, ["namespace", "assign", "JPCP", "--namespace", "myproject"])
    result = runner.invoke(app, ["namespace", "info", "myproject"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output


# --- 6. namespace list shows correct model counts ---

def test_namespace_list_shows_model_counts():
    runner.invoke(app, ["namespace", "create", "ns1"])
    runner.invoke(app, ["namespace", "assign", "JPCP", "--namespace", "ns1"])
    runner.invoke(app, ["namespace", "assign", "MACK", "--namespace", "ns1"])

    result = runner.invoke(app, ["--json", "namespace", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ns1_entry = next(d for d in data if d["name"] == "ns1")
    assert ns1_entry["models"] == 2


# --- 7. namespace assign to non-existent namespace -> error ---

def test_namespace_assign_nonexistent_namespace_errors():
    result = runner.invoke(app, ["namespace", "assign", "JPCP", "--namespace", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- 8. namespace info on missing namespace -> exit 1 ---

def test_namespace_info_missing_errors():
    result = runner.invoke(app, ["namespace", "info", "ghost"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- 9. namespace create with description ---

def test_namespace_create_with_description():
    result = runner.invoke(
        app, ["namespace", "create", "ds-team", "--description", "Data science team"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["namespace", "info", "ds-team"])
    assert result.exit_code == 0, result.output
    assert "Data science team" in result.output


# --- 10. namespace info json mode ---

def test_namespace_info_json_mode():
    runner.invoke(app, ["namespace", "create", "proj"])
    runner.invoke(app, ["namespace", "assign", "JPCP", "--namespace", "proj"])
    result = runner.invoke(app, ["--json", "namespace", "info", "proj"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["name"] == "proj"
    assert any(m["model"] == "JPCP" for m in data["models"])
