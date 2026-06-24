"""Tests for dataplane_uuid field in ModelYAMLConfig."""

from pipelines.model_loader import load_model_yaml, scan_model_yamls

_MINIMAL_YAML = """
name: TestModel
model_class: TestModel
config_class: test_config.TestConfiguration
task_type: regression
framework: sklearn
enabled: true
datasets: []
"""


def test_dataplane_uuid_parsed(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text(_MINIMAL_YAML + "dataplane_uuid: <UUID>\n")
    cfg = load_model_yaml(p)
    assert cfg.dataplane_uuid == "<UUID>"


def test_dataplane_uuid_defaults_to_none(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text(_MINIMAL_YAML)
    cfg = load_model_yaml(p)
    assert cfg.dataplane_uuid is None


def test_scan_model_yamls_includes_uuid(tmp_path):
    uid = "<UUID>"
    (tmp_path / "a.yaml").write_text(_MINIMAL_YAML + f"dataplane_uuid: {uid}\n")
    (tmp_path / "b.yaml").write_text(_MINIMAL_YAML.replace("TestModel", "OtherModel"))
    configs = scan_model_yamls(tmp_path)
    assert configs[0].dataplane_uuid == "<UUID>"
    assert configs[1].dataplane_uuid is None
