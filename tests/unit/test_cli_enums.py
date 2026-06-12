from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli._enums import (
    EnvOverlay,
    MLflowAlias,
    StackService,
    StorageBackend,
    TaskType,
    TrainType,
)


def test_task_type_values():
    assert TaskType.performance_prediction == "performance_prediction"
    assert TaskType.power_consumption_prediction == "power_consumption_prediction"
    assert TaskType.anomaly_detection == "anomaly_detection"
    assert len(TaskType) == 3


def test_train_type_values():
    assert TrainType.regression == "regression"
    assert TrainType.classification == "classification"
    assert len(TrainType) == 2


def test_env_overlay_values():
    assert EnvOverlay.dev == "dev"
    assert EnvOverlay.staging == "staging"
    assert EnvOverlay.prod == "prod"
    assert len(EnvOverlay) == 3


def test_storage_backend_values():
    assert StorageBackend.zenodo == "zenodo"
    assert StorageBackend.minio == "minio"
    assert StorageBackend.dataplane == "dataplane"
    assert len(StorageBackend) == 3


def test_mlflow_alias_values():
    assert MLflowAlias.Production == "Production"
    assert MLflowAlias.Canary == "Canary"
    assert MLflowAlias.Staging == "Staging"
    assert len(MLflowAlias) == 3


def test_stack_service_hyphenated_values():
    assert StackService.ray_serving == "ray-serving"
    assert StackService.control_plane == "control-plane"
    assert StackService.seanerbus_sim == "seanerbus-sim"
    assert StackService.seanerbus_bridge == "seanerbus-bridge"


def test_all_enums_are_str_subclass():
    for cls in (TaskType, TrainType, EnvOverlay, StorageBackend, MLflowAlias, StackService):
        for member in cls:
            assert isinstance(member, str), f"{cls.__name__}.{member.name} is not a str"


def test_stack_service_covers_all_compose_services():
    values = {s.value for s in StackService}
    for expected in [
        "postgres",
        "minio",
        "mlflow",
        "orchestrator",
        "ray-serving",
        "control-plane",
        "prometheus",
        "alertmanager",
        "tempo",
        "grafana",
        "loki",
        "promtail",
        "dashboard",
        "jupyterhub",
        "seanerbus-sim",
        "seanerbus-bridge",
    ]:
        assert expected in values, f"StackService missing: {expected}"
