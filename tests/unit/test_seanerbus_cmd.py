import uuid
import pytest
import yaml
from typer.testing import CliRunner
from examlops.cli.main import app

runner = CliRunner()


def _make_yaml(name: str, uid: str | None = None) -> str:
    base = (
        f"name: {name}\nmodel_class: {name}\n"
        f"config_class: {name.lower()}_config.{name}Configuration\n"
        f"task_type: regression\nframework: sklearn\nenabled: true\n"
    )
    if uid:
        base += f"seanerbus_uuid: {uid}\n"
    return base


def test_seanerbus_list_shows_models(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", "aaaaaaaa-0000-0000-0000-000000000001"))
    (tmp_path / "mack.yaml").write_text(_make_yaml("MACK"))
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "list"])
    assert result.exit_code == 0
    assert "JPCP" in result.output
    assert "aaaaaaaa-0000-0000-0000-000000000001" in result.output
    assert "MACK" in result.output
    assert "not assigned" in result.output


def test_init_uuids_assigns_missing_uuids(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP"))
    (tmp_path / "mack.yaml").write_text(_make_yaml("MACK", "bbbbbbbb-0000-0000-0000-000000000002"))
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "init-uuids"])
    assert result.exit_code == 0
    jpcp = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    mack = yaml.safe_load((tmp_path / "mack.yaml").read_text())
    uuid.UUID(jpcp["seanerbus_uuid"])  # validates format
    assert mack["seanerbus_uuid"] == "bbbbbbbb-0000-0000-0000-000000000002"  # unchanged


def test_init_uuids_is_idempotent(tmp_path, monkeypatch):
    existing = "cccccccc-0000-0000-0000-000000000003"
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", existing))
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "init-uuids"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    assert raw["seanerbus_uuid"] == existing  # unchanged


def test_regen_uuid_replaces_uuid(tmp_path, monkeypatch):
    old = "dddddddd-0000-0000-0000-000000000004"
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", old))
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "regen-uuid", "JPCP"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    new_uid = raw["seanerbus_uuid"]
    assert new_uid != old
    uuid.UUID(new_uid)  # valid format


def test_regen_uuid_unknown_model_exits_nonzero(tmp_path, monkeypatch):
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "regen-uuid", "NOTAMODEL"])
    assert result.exit_code != 0


def test_regen_uuid_on_model_without_uuid(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP"))  # no UUID
    import examlops.cli.commands.seanerbus_cmd as cmd
    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["seanerbus", "regen-uuid", "JPCP"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    uuid.UUID(raw["seanerbus_uuid"])  # valid format
