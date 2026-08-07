"""Dataset object store is separable from the MLflow artifact store.

Large-scale datasets (e.g. the dedicated dataset object store) may live on a
different S3/MinIO instance than the platform MinIO holding MLflow artifacts.
``EXAMLOPS_DATA_S3_*`` env vars route only the *dataset* backend; unset, the
legacy shared-instance resolution stays byte-identical.
"""

import pipelines.pipeline_generator as pg


def test_no_env_keeps_legacy_resolution(monkeypatch):
    for var in (
        "EXAMLOPS_DATA_S3_ENDPOINT",
        "EXAMLOPS_DATA_S3_ACCESS_KEY",
        "EXAMLOPS_DATA_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert pg._dataset_store_kwargs("minio") == {}


def test_dedicated_dataset_store_env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ACCESS_KEY", "AK")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_SECRET_KEY", "SK")
    assert pg._dataset_store_kwargs("minio") == {
        "endpoint_url": "https://s3.example.com",
        "access_key": "AK",
        "secret_key": "SK",
    }


def test_partial_env_only_overrides_what_is_set(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.delenv("EXAMLOPS_DATA_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATA_S3_SECRET_KEY", raising=False)
    assert pg._dataset_store_kwargs("minio") == {"endpoint_url": "https://s3.example.com"}


def test_non_minio_backends_untouched(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    assert pg._dataset_store_kwargs("dataplane") == {}


def test_minio_backend_receives_dedicated_endpoint(monkeypatch):
    """End-to-end through the real modelzoo get_backend: dataset MinIO ≠ artifact MinIO."""
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://platform-minio:9000")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ACCESS_KEY", "AK")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_SECRET_KEY", "SK")
    backend = pg._get_backend("minio", **pg._dataset_store_kwargs("minio"))
    assert backend.endpoint_url == "https://s3.example.com"
    assert backend.access_key == "AK"
