from __future__ import annotations

from enum import StrEnum


class TaskType(StrEnum):
    performance_prediction = "performance_prediction"
    power_consumption_prediction = "power_consumption_prediction"
    anomaly_detection = "anomaly_detection"


class TrainType(StrEnum):
    regression = "regression"
    classification = "classification"


class EnvOverlay(StrEnum):
    dev = "dev"
    staging = "staging"
    prod = "prod"


class StorageBackend(StrEnum):
    zenodo = "zenodo"
    minio = "minio"
    dataplane = "dataplane"


class MLflowAlias(StrEnum):
    Production = "Production"
    Canary = "Canary"
    Staging = "Staging"


class StackService(StrEnum):
    postgres = "postgres"
    minio = "minio"
    mlflow = "mlflow"
    orchestrator = "orchestrator"
    ray_serving = "ray-serving"
    control_plane = "control-plane"
    dataplane = "dataplane"
    prometheus = "prometheus"
    alertmanager = "alertmanager"
    tempo = "tempo"
    grafana = "grafana"
    loki = "loki"
    promtail = "promtail"
    dashboard = "dashboard"
    jupyterhub = "jupyterhub"
    seanerbus_sim = "seanerbus-sim"
    seanerbus_bridge = "seanerbus-bridge"
    vllm = "vllm"  # GPU-only, behind the `vllm` compose profile (ADR 0107)


class VectorIndex(StrEnum):
    """ANN index of a vector collection (ADR 0020 clause 2)."""

    flat = "flat"
    hnsw = "hnsw"
    ivfflat = "ivfflat"


class VectorSearchMode(StrEnum):
    """Retrieval channel: embedding only, BM25 only, or both fused."""

    dense = "dense"
    sparse = "sparse"
    hybrid = "hybrid"


class FusionMethod(StrEnum):
    """How hybrid search fuses the dense and sparse rankings."""

    rrf = "rrf"
    convex = "convex"
