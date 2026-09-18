# tests/unit/test_encoder_mlflow_registry.py
"""ADR 0043 clause 1 — the encoder registry as an MLflow artifact.

The recorded finding: clause 1 specifies the encoder registry as an **MLflow artifact** and it was
a `platform.db` row. With `EXAMLOPS_ENCODER_REGISTRY=mlflow` each encoder is an MLflow run whose
`encoder.json` artifact is its card; `platform.db` becomes the index the guard reads. These run a
real MLflow on a throwaway SQLite tracking store, one experiment per test.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

pytest.importorskip("mlflow")

from examlops.embeddings import (  # noqa: E402
    encoder_id,
    get_encoder,
    list_encoders,
    migrate_encoders,
    register_encoder,
    reindex,
    set_collection_encoder,
)
from examlops.embeddings import mlflow_registry as reg  # noqa: E402
from examlops.embeddings.mlflow_registry import EncoderRegistryError  # noqa: E402
from examlops.platform_db import get_encoder as local_row  # noqa: E402


@pytest.fixture(scope="module")
def mlflow_uri(tmp_path_factory):
    return f"sqlite:///{tmp_path_factory.mktemp('mlflow') / 'mlflow.db'}"


@pytest.fixture(autouse=True)
def _env(monkeypatch, mlflow_uri, tmp_path):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow_uri)
    # With a local (sqlite) tracking store MLflow's default artifact root is ./mlruns — relative to
    # wherever the process runs, which for this suite is the checkout. The platform puts artifacts
    # under the instance-data root instead (ADR 0128); giving it one keeps them in tmp.
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EXAMLOPS_ENCODER_MLFLOW_URI", raising=False)
    monkeypatch.setenv("EXAMLOPS_ENCODER_EXPERIMENT", f"encoders-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "mlflow")


def _runs():
    from mlflow import MlflowClient

    client = MlflowClient()
    exp = client.get_experiment_by_name(reg.experiment_name())
    return [] if exp is None else client.search_runs([exp.experiment_id])


# ── the record is an MLflow artifact ─────────────────────────────────────────


def test_registering_publishes_the_card_as_an_artifact():
    eid = register_encoder("e5", "2", 384, metric="dot", normalization="none")

    (run,) = _runs()
    assert run.info.status == "FINISHED" and run.info.run_name == eid
    assert run.data.params == {
        "name": "e5",
        "version": "2",
        "dim": "384",
        "metric": "dot",
        "normalization": "none",
    }
    from mlflow import MlflowClient

    path = MlflowClient().download_artifacts(run.info.run_id, "encoder.json")
    card = json.loads(Path(path).read_text())
    assert card["encoder_id"] == eid and card["dim"] == 384 and card["schema"] == reg.SCHEMA
    assert local_row(eid) is not None, "platform.db keeps the index the guard reads"


def test_registering_twice_is_one_record():
    register_encoder("bge", "1", 768)
    register_encoder("bge", "1", 768)

    assert len(_runs()) == 1


def test_mlflow_is_written_first(monkeypatch):
    """A failed publish registers nothing, so the index and the record never disagree."""

    def refuse(card, **kw):
        raise EncoderRegistryError("MLflow is down")

    monkeypatch.setattr(reg, "publish", refuse)
    with pytest.raises(EncoderRegistryError):
        register_encoder("gte", "1", 1024)

    assert local_row(encoder_id("gte", "1", 1024, "cosine", "l2")) is None


def test_without_a_tracking_uri_it_refuses(monkeypatch):
    monkeypatch.delenv("MLFLOW_TRACKING_URI")
    with pytest.raises(EncoderRegistryError, match="MLFLOW_TRACKING_URI"):
        register_encoder("minilm", "1", 384)
    assert local_row(encoder_id("minilm", "1", 384, "cosine", "l2")) is None


def test_an_unknown_registry_is_an_error_not_a_quiet_fallback(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "mlfow")
    with pytest.raises(ValueError, match="EXAMLOPS_ENCODER_REGISTRY"):
        register_encoder("minilm", "2", 384)


# ── an encoder another instance published is usable here ─────────────────────


def test_an_encoder_known_only_to_mlflow_is_pulled_into_the_index(tmp_path, monkeypatch):
    eid = encoder_id("nomic", "1.5", 768, "cosine", "l2")
    card = {
        "encoder_id": eid,
        "name": "nomic",
        "version": "1.5",
        "dim": 768,
        "metric": "cosine",
        "normalization": "l2",
    }
    reg.publish(card)  # as another instance sharing this MLflow would
    assert local_row(eid) is None

    old = register_encoder("nomic", "1.0", 768)
    set_collection_encoder("shared-docs", old)
    result = reindex("shared-docs", eid, recall=0.99)

    assert result.switched is True
    assert local_row(eid) is not None


def test_the_listing_is_mlflows():
    eid = register_encoder("jina", "3", 1024)

    rows = list_encoders()

    assert [r["encoder_id"] for r in rows] == [eid] and rows[0]["registry"] == "mlflow"


# ── what the registry refuses to serve ───────────────────────────────────────


def _raw_run(tag_id: str, params: dict, status: str = "FINISHED") -> None:
    from mlflow import MlflowClient

    client = MlflowClient()
    exp = client.get_experiment_by_name(reg.experiment_name())
    exp_id = exp.experiment_id if exp else client.create_experiment(reg.experiment_name())
    run = client.create_run(exp_id, tags={"examlops.encoder_id": tag_id})
    for k, v in params.items():
        client.log_param(run.info.run_id, k, v)
    client.set_terminated(run.info.run_id, status=status)


def test_a_record_that_does_not_hash_to_its_id_is_refused():
    """An encoder claiming one space and describing another would defeat the guard."""
    eid = encoder_id("e5", "2", 384, "cosine", "l2")
    _raw_run(
        eid,
        {"name": "e5", "version": "2", "dim": "1024", "metric": "cosine", "normalization": "l2"},
    )

    assert reg.fetch(eid) is None and reg.fetch_all() == []
    assert get_encoder(eid) is None


def test_a_publish_that_did_not_finish_is_never_read():
    eid = encoder_id("e5", "3", 384, "cosine", "l2")
    fields = {"name": "e5", "version": "3", "dim": "384", "metric": "cosine", "normalization": "l2"}
    _raw_run(eid, fields, status="FAILED")

    assert reg.fetch(eid) is None


def test_a_hostile_id_cannot_widen_the_lookup():
    """The id goes into an MLflow filter string; a quote in it must not end the literal."""
    register_encoder("e5", "4", 384)

    assert reg.fetch("x' OR tags.`examlops.encoder_id` != '") is None


# ── migration ────────────────────────────────────────────────────────────────


def test_migrate_publishes_local_encoders_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "local")
    a = register_encoder("loc-a", "1", 128)
    b = register_encoder("loc-b", "1", 256)
    assert _runs() == [], "the local registry touches no MLflow"

    dry = migrate_encoders(dry_run=True)
    assert set(dry["published"]) >= {a, b} and _runs() == []

    first = migrate_encoders()
    assert set(first["published"]) >= {a, b}
    second = migrate_encoders()
    assert second["published"] == [] and set(second["skipped"]) >= {a, b}


def test_the_local_registry_is_unchanged(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "local")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")  # never contacted

    eid = register_encoder("offline", "1", 64)

    assert get_encoder(eid) is not None
    assert {r["registry"] for r in list_encoders()} == {"local"}


def test_the_cli_migrates_and_lists(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "local")
    eid = register_encoder("cli-enc", "1", 32)
    monkeypatch.setenv("EXAMLOPS_ENCODER_REGISTRY", "mlflow")

    r = CliRunner().invoke(app, ["--json", "embedding", "migrate"])
    assert r.exit_code == 0, r.output
    assert eid in json.loads(r.stdout)["published"]

    r = CliRunner().invoke(app, ["--json", "embedding", "list"])
    assert r.exit_code == 0, r.output
    assert {e["encoder_id"]: e["registry"] for e in json.loads(r.stdout)}[eid] == "mlflow"
