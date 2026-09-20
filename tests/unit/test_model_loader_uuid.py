"""Tests for dataplane_bus_uuid field in ModelYAMLConfig."""

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


def test_dataplane_bus_uuid_parsed(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text(_MINIMAL_YAML + "dataplane_bus_uuid: f47ac10b-58cc-4372-a567-0e02b2c3d479\n")
    cfg = load_model_yaml(p)
    assert cfg.dataplane_bus_uuid == "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_dataplane_bus_uuid_defaults_to_none(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text(_MINIMAL_YAML)
    cfg = load_model_yaml(p)
    assert cfg.dataplane_bus_uuid is None


def test_scan_model_yamls_includes_uuid(tmp_path):
    uid = "aaaaaaaa-0000-0000-0000-000000000001"
    (tmp_path / "a.yaml").write_text(_MINIMAL_YAML + f"dataplane_bus_uuid: {uid}\n")
    (tmp_path / "b.yaml").write_text(_MINIMAL_YAML.replace("TestModel", "OtherModel"))
    configs = scan_model_yamls(tmp_path)
    assert configs[0].dataplane_bus_uuid == "aaaaaaaa-0000-0000-0000-000000000001"
    assert configs[1].dataplane_bus_uuid is None
