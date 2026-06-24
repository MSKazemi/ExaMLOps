import uuid

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
        base += f"dataplane_uuid: {uid}\n"
    return base


def test_dataplane_list_shows_models(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", "<UUID>"))
    (tmp_path / "mack.yaml").write_text(_make_yaml("MACK"))
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "list"])
    assert result.exit_code == 0
    assert "JPCP" in result.output
    assert "<UUID>" in result.output
    assert "MACK" in result.output
    assert "not assigned" in result.output


def test_init_uuids_assigns_missing_uuids(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP"))
    (tmp_path / "mack.yaml").write_text(_make_yaml("MACK", "<UUID>"))
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "init-uuids"])
    assert result.exit_code == 0
    jpcp = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    mack = yaml.safe_load((tmp_path / "mack.yaml").read_text())
    uuid.UUID(jpcp["dataplane_uuid"])  # validates format
    assert mack["dataplane_uuid"] == "<UUID>"  # unchanged


def test_init_uuids_is_idempotent(tmp_path, monkeypatch):
    existing = "<UUID>"
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", existing))
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "init-uuids"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    assert raw["dataplane_uuid"] == existing  # unchanged


def test_regen_uuid_replaces_uuid(tmp_path, monkeypatch):
    old = "<UUID>"
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP", old))
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "regen-uuid", "JPCP"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    new_uid = raw["dataplane_uuid"]
    assert new_uid != old
    uuid.UUID(new_uid)  # valid format


def test_regen_uuid_unknown_model_exits_nonzero(tmp_path, monkeypatch):
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "regen-uuid", "NOTAMODEL"])
    assert result.exit_code != 0


def test_regen_uuid_on_model_without_uuid(tmp_path, monkeypatch):
    (tmp_path / "jpcp.yaml").write_text(_make_yaml("JPCP"))  # no UUID
    import examlops.cli.commands.dataplane_cmd as cmd

    monkeypatch.setattr(cmd, "MODELS_DIR", tmp_path)
    result = runner.invoke(app, ["dataplane", "regen-uuid", "JPCP"])
    assert result.exit_code == 0
    raw = yaml.safe_load((tmp_path / "jpcp.yaml").read_text())
    uuid.UUID(raw["dataplane_uuid"])  # valid format
