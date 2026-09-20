"""``exa config export`` — one-file generated snapshot of all platform config."""

from examlops.cli.commands.config_cmd import _redact, build_export_snapshot


def test_snapshot_has_all_sections(monkeypatch):
    snap = build_export_snapshot()
    for section in (
        "_meta",
        "cli",
        "hpc",
        "object_stores",
        "models",
        "env_overlays",
        "environment",
    ):
        assert section in snap
    assert snap["cli"]["settings"]  # effective config resolved
    assert "artifact_store" in snap["object_stores"]
    assert "dataset_store" in snap["object_stores"]


def test_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_S3_SECRET_KEY", "supersecret")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "tok123")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    snap = build_export_snapshot()
    env = snap["environment"]
    assert env["EXAMLOPS_DATA_S3_SECRET_KEY"] == "***"
    assert env["CONTROL_PLANE_TOKEN"] == "***"
    # Non-secret values pass through untouched.
    assert env["EXAMLOPS_DATA_S3_ENDPOINT"] == "https://s3.example.com"
    # Secret VALUE never appears anywhere in the serialized snapshot.
    import yaml

    assert "supersecret" not in yaml.safe_dump(snap)


def test_dataset_store_section_reflects_split(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "https://s3.example.com")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ACCESS_KEY", "AK")
    monkeypatch.setenv("EXAMLOPS_DATA_BUCKET", "other-dataset")
    ds = build_export_snapshot()["object_stores"]["dataset_store"]
    assert ds["endpoint"] == "https://s3.example.com"
    assert ds["bucket"] == "other-dataset"
    assert ds["credentials_set"] is True


def test_redact_helper():
    assert _redact("control_plane_token", "abc") == "***"
    assert _redact("AWS_SECRET_ACCESS_KEY", "abc") == "***"
    assert _redact("mlflow_url", "http://x") == "http://x"
    assert _redact("SOME_PASSWORD", "") == ""  # empty stays empty
