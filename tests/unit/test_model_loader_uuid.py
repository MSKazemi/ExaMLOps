
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


    p = tmp_path / "test.yaml"
    cfg = load_model_yaml(p)


    p = tmp_path / "test.yaml"
    p.write_text(_MINIMAL_YAML)
    cfg = load_model_yaml(p)


def test_scan_model_yamls_includes_uuid(tmp_path):
    uid = "aaaaaaaa-0000-0000-0000-000000000001"
    (tmp_path / "b.yaml").write_text(_MINIMAL_YAML.replace("TestModel", "OtherModel"))
    configs = scan_model_yamls(tmp_path)
