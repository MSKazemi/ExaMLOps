"""ADR 0130 §8 — the flow uses the pin for the gate, the MLflow tags and the HPC job."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(REPO_ROOT),
    str(REPO_ROOT / "platform" / "cli" / "src"),
    str(REPO_ROOT / "modelzoo"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pg = pytest.importorskip("pipelines.pipeline_generator")  # needs the use-case pack + modelzoo
from examlops.data import data_assets  # noqa: E402
from examlops.dataplane import store as st  # noqa: E402
from examlops.dataplane.types import Limits, TableBatch  # noqa: E402
from pipelines.datasets import dataplane as ad  # noqa: E402


@pytest.fixture
def pin(tmp_path):
    local = tmp_path / "rev1"
    (local / "job_table").mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([{"a": 1}, {"a": 2}]), local / "job_table" / "part-00000.parquet"
    )
    p = ad.SnapshotPin("_global/pm100", "rev1" * 16, local, ("job_table",), "file:///m.json")
    ad.reset_pins()
    ad._PINS[("JPCP", "PM100Dataset")] = p
    yield p
    ad.reset_pins()


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A real dataset store holding one `_global/pm100` snapshot; returns its revision."""
    url = f"file://{tmp_path / 'store'}"
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("EXAMLOPS_DATASET_REVISION", raising=False)
    ad.reset_pins()
    w = st.SnapshotWriter(tmp_path / "p1", limits=Limits())
    w.write(TableBatch("job_table", pa.RecordBatch.from_pylist([{"v": 1}, {"v": 2}])))
    ref = st.publish(
        st.DatasetStore.from_url(url),
        "_global/pm100",
        staged=w.close(),
        parent=None,
        connector="sql",
        connection=None,
        spec_hash="h",
        watermark={},
        pull_id="p1",
        incremental=False,
    )[0]
    yield ref.revision
    ad.reset_pins()


def _jpcp():
    """The real JPCP YAML + shim, with PM100Dataset bound to the `pm100` dataplane source."""
    cfg = pg.MODEL_REGISTRY["JPCP"][1]
    entries = [
        dataclasses.replace(d, dataplane={"source": "pm100"}) if d.name == "PM100Dataset" else d
        for d in cfg._yaml.datasets
    ]
    return dataclasses.replace(cfg._yaml, datasets=entries, project=None), cfg._shim


def _build(monkeypatch, backend_name, *, is_dummy):
    """Run `_build_train_components` with a recording dataset class; returns the dataset kwargs."""
    captured: dict = {}

    class PM100Dataset:  # the name is what the YAML lookup keys on
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(pg, "_Dataloader", lambda dataset, **_: dataset)
    monkeypatch.setattr(
        pg, "_get_backend", lambda *a, **k: pytest.fail(f"pack get_backend used: {a}")
    )
    yaml_cfg, shim = _jpcp()
    pg._build_train_components(yaml_cfg, shim, PM100Dataset, "train", is_dummy, backend_name)
    return captured


def _seed_pull_row(revision: str) -> None:
    """The `dataset_revisions` row a dataplane pull writes (examlops.dataplane.pull)."""
    data_assets.record_dataset_revision(
        SimpleNamespace(
            backend="dataplane",
            dataset="_global/pm100",
            revision_id=revision,
            kind="dataplane",
            uri="file:///m.json",
            schema_hash="",
        )
    )


def _linked_run(revision: str) -> str | None:
    row = data_assets.get_dataset_revision("_global/pm100", revision, "dataplane")
    assert row is not None
    return row["mlflow_run_id"]


# ── the contract gate ──────────────────────────────────────────────────────────


def test_contract_dataframe_reads_the_pinned_copy(pin):
    df, source = pg._contract_dataframe("PM100Dataset", "dataplane", model_name="JPCP")
    assert len(df) == 2 and source == str(pin.local_dir)


def test_contract_dataframe_ignores_a_leftover_pin_for_a_minio_run(pin):
    _, source = pg._contract_dataframe("PM100Dataset", "minio", model_name="JPCP")
    assert source != str(pin.local_dir)


# ── MLflow tags + the revision link ────────────────────────────────────────────


def test_revision_pin_tags_the_run_and_links_the_pull_row(pin, monkeypatch):
    _seed_pull_row(pin.revision)
    tags: dict = {}
    monkeypatch.setattr(pg.mlflow, "set_tag", lambda k, v: tags.__setitem__(k, v))
    pg._pin_dataset_revision("PM100Dataset", "dataplane", "run-1", model_name="JPCP")
    assert tags["dataset_revision"] == pin.revision
    assert tags["dataset_backend"] == "dataplane" and tags["dataset_uri"] == pin.manifest_uri
    assert (
        tags["dataplane.source"] == "_global/pm100" and tags["dataplane.revision"] == pin.revision
    )
    assert _linked_run(pin.revision) == "run-1"


@pytest.mark.parametrize(
    ("backend", "is_dummy"), [("minio", False), ("dataplane", True)], ids=["minio", "dummy"]
)
def test_a_leftover_pin_never_tags_or_links(pin, monkeypatch, backend, is_dummy):
    """The pinned snapshot is never claimed: no `dataplane.*` tags, not its revision, no link.

    The run still gets the ordinary A1 tags (the requested backend at revision `unknown`), as
    any dummy or unmaterialised run does.
    """
    _seed_pull_row(pin.revision)
    tags: dict = {}
    monkeypatch.setattr(pg.mlflow, "set_tag", lambda k, v: tags.__setitem__(k, v))
    pg._pin_dataset_revision("PM100Dataset", backend, "run-9", model_name="JPCP", is_dummy=is_dummy)
    assert not any(k.startswith("dataplane.") for k in tags)
    assert tags["dataset_revision"] != pin.revision
    assert tags["dataset_uri"] != pin.manifest_uri
    assert _linked_run(pin.revision) is None


def test_link_dataset_revision_run_first_run_wins():
    _seed_pull_row("r" * 64)
    data_assets.link_dataset_revision_run("dataplane", "_global/pm100", "r" * 64, "run-A")
    data_assets.link_dataset_revision_run("dataplane", "_global/pm100", "r" * 64, "run-B")
    assert _linked_run("r" * 64) == "run-A"


def test_link_dataset_revision_run_ignores_an_empty_run_id():
    _seed_pull_row("s" * 64)
    data_assets.link_dataset_revision_run("dataplane", "_global/pm100", "s" * 64, "")
    assert _linked_run("s" * 64) is None
    data_assets.link_dataset_revision_run("dataplane", "_global/pm100", "s" * 64, "run-A")
    assert _linked_run("s" * 64) == "run-A"


# ── dataset construction routing ───────────────────────────────────────────────


def test_missing_binding_names_model_and_dataset():
    class _Entry:
        name, backend, dataplane = "PM100Dataset", "dataplane", None

    class _Cfg:
        name, project = "JPCP", None

    with pytest.raises(ValueError, match="JPCP.*PM100Dataset.*datasets\\[\\]\\.dataplane"):
        pg._dataplane_backend(_Cfg(), _Entry())


@pytest.mark.parametrize("backend", ["dataplane", "Dataplane", " DATAPLANE "])
def test_dataplane_routes_to_the_pinned_snapshot_never_the_pack(published, monkeypatch, backend):
    kwargs = _build(monkeypatch, backend, is_dummy=False)
    assert isinstance(kwargs["backend"], ad.DataplaneDatasetBackend)
    assert kwargs["backend"].pin.revision == published
    assert ad.current_pin("JPCP", "PM100Dataset").revision == published
    assert "use_zenodo_url" not in kwargs


@pytest.mark.parametrize("backend", ["dataplane", "Dataplane"])
def test_a_dummy_dataplane_run_never_pins(monkeypatch, backend):
    ad.reset_pins()
    monkeypatch.setattr(ad, "pin_for", lambda *a, **k: pytest.fail("dummy run pinned"))
    kwargs = _build(monkeypatch, backend, is_dummy=True)
    assert kwargs.get("use_zenodo_url") is True and "backend" not in kwargs
    assert ad.current_pin("JPCP", "PM100Dataset") is None


# ── the HPC job ────────────────────────────────────────────────────────────────


def _hpc(backend_name, *, is_dummy=False):
    return pg._hpc_train_command(
        "JPCP",
        "PM100Dataset",
        is_dummy=is_dummy,
        backend_name=backend_name,
        remote_model="/r/model.pkl",
        mlflow_uri="http://m",
    )


def test_hpc_script_receives_backend_and_revision(pin):
    script = _hpc("dataplane")
    assert "--backend dataplane" in script and f"--dataset-revision {pin.revision}" in script


def test_hpc_pin_wins_when_the_yaml_default_is_dataplane(pin, monkeypatch):
    entry = pg.MODEL_REGISTRY["JPCP"][1]._yaml.dataset("PM100Dataset")
    monkeypatch.setattr(entry, "backend", "dataplane")
    script = _hpc(None)
    assert f"--backend dataplane --dataset-revision {pin.revision}" in script


def test_hpc_ignores_a_leftover_pin_when_the_run_uses_minio(pin):
    script = _hpc("minio")
    assert "--backend minio" in script and "--dataset-revision" not in script
    assert "--backend dataplane" not in script


def test_hpc_dummy_run_never_forwards_a_revision(pin):
    script = _hpc("dataplane", is_dummy=True)
    assert "--dummy" in script and "--dataset-revision" not in script


# ── pin lifetime ───────────────────────────────────────────────────────────────


def test_forget_pin_drops_one_pin_with_pin_for_key_normalisation(pin):
    other = dataclasses.replace(pin, source_key="_global/fdata")
    ad._PINS[("MACK", "FDataDataset")] = other
    ad.forget_pin("jpcp", "PM100Dataset")  # same upper-casing as pin_for
    assert ad.current_pin("JPCP", "PM100Dataset") is None
    assert ad.current_pin("MACK", "FDataDataset") == other
    ad.forget_pin("jpcp", "PM100Dataset")  # idempotent


def test_training_flow_starts_without_the_previous_runs_pin(pin, monkeypatch):
    seen: dict = {}

    def extraction(model, dataset, is_dummy, backend):
        seen["pin_at_start"] = ad.current_pin(model, dataset)
        return None, None

    def submit(*a, **k):
        seen["submit_backend"] = k.get("backend_name")
        return "job", "artifact"

    def log(*a, **k):
        seen["log_is_dummy"] = k.get("is_dummy")
        return {"version": None, "run_id": None}

    monkeypatch.setattr(pg, "data_extraction_task", extraction)
    monkeypatch.setattr(pg, "data_contract_gate", lambda *a, **k: {"validated": False})
    monkeypatch.setattr(pg, "slurm_submit_task", submit)
    monkeypatch.setattr(pg, "slurm_wait_task", lambda *a: ("COMPLETED", "artifact"))
    monkeypatch.setattr(pg, "result_fetch_task", lambda *a: object())
    monkeypatch.setattr(pg, "evaluate_task", lambda *a: {"rmse": 1.0})
    monkeypatch.setattr(pg, "log_mlflow_task", log)
    monkeypatch.setattr(pg, "promote_task", lambda *a: "Staging")

    pg.training_flow.fn("JPCP", "PM100Dataset", is_dummy=True, backend_name="minio")
    assert seen == {"pin_at_start": None, "submit_backend": "minio", "log_is_dummy": True}
