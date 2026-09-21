"""Offline (batch) inference — ADR 0149.

Every test runs the real path: a real sklearn model with an MLflow column signature registered in
a real (sqlite) MLflow registry, loaded by the serving replica's own loader, over real Parquet,
with a real sqlite ``platform.db`` (the suite's per-test one). The two seams the executor exposes
(``loader``) are used only to *wrap* the real model in something that counts calls, never to
replace inference.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pd = pytest.importorskip("pandas")
mlflow = pytest.importorskip("mlflow")
sklearn_linear = pytest.importorskip("sklearn.linear_model")

from examlops import offline, operations  # noqa: E402
from examlops.data import offline as offline_store  # noqa: E402
from examlops.data.content_hash import (  # noqa: E402
    file_digest,
    revision_id,
    schema_hash,
    schema_part,
)
from examlops.offline import OfflineJob, OfflineSpecError  # noqa: E402

COLUMNS = ["cpu", "mem", "nodes"]
MODEL = "offline-lin"


# ── fixtures ───────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    """A registered sklearn model (two versions; ``Production`` -> 2) in a sqlite MLflow."""
    from mlflow.models import infer_signature

    root = tmp_path_factory.mktemp("mlflow")
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{root}/mlflow.db")
    exp = mlflow.create_experiment("offline", artifact_location=(root / "art").as_uri())
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.random((30, 3)), columns=COLUMNS)
    y = X["cpu"] + 2 * X["mem"] + 3 * X["nodes"]
    for k in (1, 2):  # v1 is deliberately a different function from v2
        model = sklearn_linear.LinearRegression().fit(X, y * k)
        with mlflow.start_run(experiment_id=exp):
            mlflow.sklearn.log_model(
                model, name="model", signature=infer_signature(X, y), registered_model_name=MODEL
            )
    mlflow.MlflowClient().set_registered_model_alias(MODEL, "Production", "2")
    yield MODEL
    mlflow.set_tracking_uri(previous)


def _frame(n: int, seed: int = 1) -> pd.DataFrame:
    df = pd.DataFrame(np.random.default_rng(seed).random((n, 3)), columns=COLUMNS)
    df["job_id"] = [f"j{i}" for i in range(n)]  # an id column the model must ignore
    return df


def _write_input(path: Path, n: int = 250, seed: int = 1) -> pd.DataFrame:
    df = _frame(n, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return df


def _spec(tmp_path: Path, key: str = "k1", **over) -> OfflineJob:
    doc = {
        "model": MODEL,
        "version": "2",
        "input": {"type": "local", "path": str(tmp_path / "in.parquet")},
        "output": {"type": "local", "path": str(tmp_path / "out")},
        "batch_size": 100,
        "idempotency_key": key,
    }
    doc.update(over)
    return OfflineJob.load(doc)


def _read_out(result: dict) -> pd.DataFrame:
    parts = sorted((Path(result["output"]["uri"]) / "predictions").glob("*.parquet"))
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)


class _Counting:
    """Wraps the *real* pyfunc model and counts predict calls; everything else is delegated."""

    def __init__(self, model):
        self._model, self.calls = model, 0

    def predict(self, x):
        self.calls += 1
        return self._model.predict(x)

    def __getattr__(self, name):
        return getattr(self._model, name)


def _counting_loader(box: list):
    from serving.ray_serving.app import load_model_version

    def load(model: str, version: str):
        mv = mlflow.MlflowClient().get_model_version(model, version)
        wrapped = _Counting(load_model_version(model, None, mv))
        box.append(wrapped)
        return wrapped

    return load


class _Crash(BaseException):
    """A process death: not an ``Exception``, so nothing in the executor can absorb it."""


# ── the spec ───────────────────────────────────────────────────────────────────────────────
def _doc(**over):
    doc = {
        "model": "m",
        "version": "3",
        "input": {"type": "local", "path": "/x"},
        "output": {"type": "local", "path": "/y"},
        "idempotency_key": "k",
    }
    doc.update(over)
    return doc


def test_a_valid_spec_round_trips():
    spec = OfflineJob.load(_doc())
    assert OfflineJob.load(spec.to_dict()) == spec
    assert spec.kind == "predictive" and spec.batch_size == 1000 and spec.schema_version == 1


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"version": None}, "exactly one of version or alias"),
        ({"alias": "Production"}, "exactly one of version or alias"),
        ({"version": "v3"}, "version must be a registry version number"),
        ({"idempotency_key": ""}, "idempotency_key is required"),
        ({"idempotency_key": "has space"}, "idempotency_key is required"),
        ({"batch_size": 0}, "batch_size must be an integer"),
        ({"batch_size": True}, "batch_size must be an integer"),
        ({"kind": "batch"}, "kind must be one of"),
        ({"resources": {"gpus": -1}}, "resources.gpus"),
        ({"flexibility_s": 60}, "carbon-aware"),
        ({"schema_version": 2}, "schema_version"),
        ({"input": {"type": "dataplane", "source": "s"}}, "input.table"),
        (
            {"input": {"type": "dataplane", "source": "s", "table": "t", "revision": "abc"}},
            "64-hex",
        ),
        ({"input": {"type": "local"}}, "input.path is required"),
        ({"output": {"type": "local"}}, "output.path is required"),
        ({"output": {"type": "dataplane"}}, "output.source"),
    ],
)
def test_invalid_specs_are_refused_with_the_reason(over, needle):
    with pytest.raises(OfflineSpecError) as exc:
        OfflineJob.load(_doc(**over))
    assert any(needle in p for p in exc.value.problems), exc.value.problems


def test_every_problem_is_listed_not_just_the_first():
    with pytest.raises(OfflineSpecError) as exc:
        OfflineJob.load(_doc(version=None, batch_size=0, idempotency_key=""))
    assert len(exc.value.problems) >= 3


def test_an_unknown_key_is_an_error_not_ignored():
    with pytest.raises(OfflineSpecError, match="unknown key batchsize"):
        OfflineJob.load(_doc(batchsize=5))
    with pytest.raises(OfflineSpecError, match="unknown key input.pth"):
        OfflineJob.load(_doc(input={"type": "local", "pth": "/x"}))
    with pytest.raises(OfflineSpecError, match="JSON object"):
        OfflineJob.from_dict([1])
    with pytest.raises(OfflineSpecError, match="idempotency_key"):
        OfflineJob.from_dict({"model": "m"})  # a missing required field is a spec error too


def test_run_refuses_an_invalid_spec_without_touching_anything(tmp_path):
    bad = OfflineJob.from_dict(_doc(version=None))
    out = offline.run(bad)
    assert out["ok"] is False and out["code"] == "invalid_spec" and out["problems"]


@pytest.mark.parametrize("kind", ["generative", "agentic"])
def test_unsupported_kinds_are_refused_not_faked(tmp_path, registry, kind):
    _write_input(tmp_path / "in.parquet", 10)
    out = offline.run(_spec(tmp_path, kind=kind))
    assert out["ok"] is False and out["code"] == "kind_not_supported_offline"
    assert "not built" in out["error"]
    assert offline_store.list_jobs() == []  # no job row, no output
    assert not (tmp_path / "out").exists()


# ── the happy path: the same inference as online ────────────────────────────────────────────
def test_predictive_run_matches_the_model_and_the_online_oip_route(tmp_path, registry):
    df = _write_input(tmp_path / "in.parquet", 250)
    box: list = []
    res = offline.run(_spec(tmp_path), loader=_counting_loader(box))
    assert res["ok"] is True and res["state"] == "completed" and res["replayed"] is False
    assert res["counts"] == {
        "batches": 3,
        "skipped_batches": 0,
        "rows": 250,
        "rows_ok": 250,
        "rows_err": 0,
    }
    assert box[0].calls == 3  # 250 rows in bounded batches of 100: never the whole dataset at once

    out = _read_out(res)
    assert list(out["row_id"]) == list(range(250))
    assert out["error"].isna().all()
    real = mlflow.pyfunc.load_model(f"models:/{MODEL}/2")
    expected = real.predict(df[COLUMNS])
    np.testing.assert_allclose(out["prediction"].to_numpy(), expected)
    assert not np.allclose(out["prediction"].to_numpy(), real.predict(df[COLUMNS]) / 2)  # v2 not v1

    # ADR 0149 verification 4: the same inputs scored ONLINE, through the replica's own /v2 route
    # (hand-built server, as test_oip_v2 builds it), agree with the offline output.
    from serving.ray_serving import app as rs_app

    srv = object.__new__(rs_app.MultiModelServer.func_or_class)
    srv._cache_lock = threading.RLock()
    srv._hot = {("jpcp", "Production"): {"model": real, "version": "2", "run_id": "r"}}
    srv._version_cache = OrderedDict()
    srv._version_cache_size = 8
    srv._preload_aliases = list(rs_app.PRELOAD_ALIASES)
    for attr in ("_req_counter", "_latency_hist", "_pred_value_hist", "_version_gauge"):
        setattr(srv, attr, MagicMock())
    srv._predict_pool = ThreadPoolExecutor(max_workers=2)
    srv._predict_timeout = 30.0
    srv._mirror = lambda *a, **k: None
    try:
        head = df.head(100)
        body = {
            "inputs": [
                {"name": c, "shape": [100], "datatype": "FP64", "data": head[c].tolist()}
                for c in COLUMNS
            ]
        }
        online = srv.v2_infer("jpcp", body=body)
        online = json.loads(online.body) if hasattr(online, "body") else online
        online_pred = np.asarray(online["outputs"][0]["data"], dtype=float)
    finally:
        srv._predict_pool.shutdown(wait=False)
    np.testing.assert_allclose(out["prediction"].to_numpy()[:100], online_pred, rtol=1e-9)


def test_an_alias_is_resolved_once_to_an_immutable_version(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 20)
    res = offline.run(_spec(tmp_path, version=None, alias="Production"))
    assert res["ok"] and res["model"] == {"name": MODEL, "version": "2", "alias": "Production"}
    row = offline_store.get(res["job_id"])
    assert row["model_version"] == "2" and json.loads(row["spec_json"])["version"] == "2"


def test_manifest_records_input_model_engine_counts_and_batches(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 250)
    res = offline.run(_spec(tmp_path))
    dest = Path(res["output"]["uri"])
    manifest = json.loads((dest / "_manifest.json").read_text())
    assert dest.name == manifest["revision"] == res["output"]["revision"]
    assert manifest["model"] == {"name": MODEL, "version": "2", "alias": None}
    assert (
        manifest["input"]["revision"] == res["input"]["revision"]
        and len(manifest["input"]["revision"]) == 64
    )
    assert manifest["engine"]["executor"] == "mlflow-pyfunc-oip" and manifest["engine"]["mlflow"]
    assert manifest["parameters"] == {"batch_size": 100, "workload_kind": "predictive"}
    assert manifest["counts"]["rows_ok"] == 250
    assert [b["rows"] for b in manifest["batches"]] == [100, 100, 50]
    assert all(b["errors"] == 0 and len(b["sha256"]) == 64 for b in manifest["batches"])
    assert not list((dest / "predictions").glob("*.json"))  # sidecars are not part of the output
    assert not (tmp_path / "out" / ".work" / res["job_id"]).exists()  # staging cleaned up


def test_output_revision_is_content_addressed(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 250)
    a = offline.run(_spec(tmp_path, key="a"))
    b = offline.run(
        _spec(tmp_path, key="b", output={"type": "local", "path": str(tmp_path / "o2")})
    )
    assert a["output"]["revision"] == b["output"]["revision"]  # same input+model => same bytes
    parts = sorted((Path(a["output"]["uri"]) / "predictions").glob("*.parquet"))
    again = revision_id([file_digest(p) for p in parts], schema_hash(schema_part(p) for p in parts))
    assert again == a["output"]["revision"]  # it is the dataplane's own formula over the files
    # a different model version => different predictions => a different revision
    v1 = offline.run(_spec(tmp_path, key="c", version="1"))
    assert v1["output"]["revision"] != a["output"]["revision"]
    # different input => different input revision
    _write_input(tmp_path / "in2.parquet", 250, seed=9)
    other = offline.run(
        _spec(tmp_path, key="d", input={"type": "local", "path": str(tmp_path / "in2.parquet")})
    )
    assert other["input"]["revision"] != a["input"]["revision"]
    assert other["output"]["revision"] != a["output"]["revision"]


# ── idempotency, resume, cancel ────────────────────────────────────────────────────────────
def test_a_completed_job_is_replayed_and_computes_nothing(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 250)
    first = offline.run(_spec(tmp_path))
    box: list = []
    again = offline.run(_spec(tmp_path), loader=_counting_loader(box))
    assert again["replayed"] is True and again["output"] == first["output"]
    assert box == []  # the model was not even loaded
    assert len(offline_store.list_jobs()) == 1


def test_the_same_key_for_a_different_request_is_refused(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 50)
    offline.run(_spec(tmp_path))
    out = offline.run(_spec(tmp_path, batch_size=50))
    assert out["ok"] is False and out["code"] == "idempotency_conflict"
    out = offline.run(_spec(tmp_path, version="1"))
    assert out["code"] == "idempotency_conflict"


def test_resume_after_a_crash_skips_every_committed_batch(tmp_path, registry, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OFFLINE_LEASE_TTL", "0.05")
    df = _write_input(tmp_path / "in.parquet", 450)  # 5 batches of 100 (last 50)

    def die_after_two(info):
        if info["idx"] == 1:
            raise _Crash

    with pytest.raises(_Crash):
        offline.run(_spec(tmp_path), on_batch=die_after_two)
    job_id = offline.job_id_for("default", "k1")
    row = offline_store.get(job_id)
    assert row["state"] == "running" and row["batches_done"] == 2  # a crashed runner's footprint
    work = tmp_path / "out" / ".work" / job_id / "predictions"
    committed = {p.name: p.stat().st_mtime_ns for p in work.glob("*.parquet")}
    assert set(committed) == {"part-00000.parquet", "part-00001.parquet"}

    # While the crashed runner's lease is live a second runner is refused; once it lapses the same
    # command takes the job over. (A runner that took it over holds a long lease: also refused.)
    time.sleep(0.1)
    taken = offline_store.claim(
        job_id,
        tenant="default",
        key="k1",
        spec_hash=row["spec_hash"],
        spec={},
        kind="predictive",
        model=MODEL,
        model_version="2",
        actor="x",
        lease_ttl_s=60,
    )
    assert taken[0] == "resumed"
    assert offline.run(_spec(tmp_path))["code"] == "idempotency_in_progress"
    offline_store.finish(job_id, "failed", error="reset for the test")  # release the long lease

    box: list = []
    res = offline.run(_spec(tmp_path), loader=_counting_loader(box))
    assert res["ok"] and res["resumed"] is True
    assert res["counts"]["skipped_batches"] == 2 and res["counts"]["rows_ok"] == 450
    assert box[0].calls == 3  # only the three uncommitted batches were scored
    out = _read_out(res)
    real = mlflow.pyfunc.load_model(f"models:/{MODEL}/2")
    np.testing.assert_allclose(out["prediction"].to_numpy(), real.predict(df[COLUMNS]))
    assert offline_store.get(job_id)["attempts"] == 3


def test_a_torn_part_is_recomputed_not_trusted(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 250)
    seen: list = []

    def fail_on_third(info):
        seen.append(info["idx"])
        if info["idx"] == 2:
            raise RuntimeError("disk full")  # an Exception: recorded as failed, parts kept

    # on_batch errors propagate as a failed run; committed parts remain for the resume
    out = offline.run(_spec(tmp_path), on_batch=fail_on_third)
    assert out["ok"] is False and out["state"] == "failed"
    job_id = out["job_id"]
    part = tmp_path / "out" / ".work" / job_id / "predictions" / "part-00000.parquet"
    part.write_bytes(part.read_bytes()[:-9] + b"corrupted")  # altered after its sidecar was written
    box: list = []
    res = offline.run(_spec(tmp_path), loader=_counting_loader(box))
    assert res["ok"] and res["counts"]["skipped_batches"] == 2  # batches 1 and 2; 0 was torn
    assert box[0].calls == 1
    assert res["counts"]["rows_ok"] == 250


def test_cancel_stops_after_the_batch_in_flight_and_a_rerun_resumes(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 450)
    job_id = offline.job_id_for("default", "k1")

    def cancel_at_first(info):
        if info["idx"] == 0:
            out = offline.cancel(job_id)
            assert out["ok"] and out["outcome"] == "requested" and out["cancelled"] is False

    res = offline.run(_spec(tmp_path), on_batch=cancel_at_first)
    assert res["ok"] is False and res["state"] == "cancelled" and res["counts"]["batches"] == 1
    assert offline.status(job_id)["job"]["state"] == "cancelled"
    assert offline.cancel(job_id)["code"] == "not_cancellable"
    again = offline.run(_spec(tmp_path))
    assert (
        again["ok"]
        and again["counts"]["skipped_batches"] == 1
        and again["counts"]["rows_ok"] == 450
    )


def test_cancelling_a_stalled_job_is_immediate(tmp_path, registry, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OFFLINE_LEASE_TTL", "0.05")
    _write_input(tmp_path / "in.parquet", 250)
    with pytest.raises(_Crash):
        offline.run(_spec(tmp_path), on_batch=lambda i: (_ for _ in ()).throw(_Crash()))
    job_id = offline.job_id_for("default", "k1")
    time.sleep(0.1)
    assert offline.status(job_id)["job"]["state"] == "stalled"
    out = offline.cancel(job_id)
    assert out["cancelled"] is True and out["job"]["state"] == "cancelled"
    assert offline.cancel("off-nope")["code"] == "not_found"


# ── bad data ───────────────────────────────────────────────────────────────────────────────
def test_a_bad_row_costs_one_row_and_is_tallied(tmp_path, registry):
    df = _frame(250)
    df.loc[[5, 120, 121], "mem"] = None  # nulls the model cannot take
    df.to_parquet(tmp_path / "in.parquet")
    res = offline.run(_spec(tmp_path))
    assert res["ok"] and res["counts"]["rows_ok"] == 247 and res["counts"]["rows_err"] == 3
    out = _read_out(res)
    bad = out[out["error"].notna()]
    assert list(bad["row_id"]) == [5, 120, 121] and bad["prediction"].isna().all()
    assert "NaN" in bad["error"].iloc[0]  # the model's own refusal, verbatim
    good = out[out["error"].isna()]
    real = mlflow.pyfunc.load_model(f"models:/{MODEL}/2")
    np.testing.assert_allclose(
        good["prediction"].to_numpy(), real.predict(df.drop(index=[5, 120, 121])[COLUMNS])
    )
    manifest = json.loads((Path(res["output"]["uri"]) / "_manifest.json").read_text())
    assert [b["errors"] for b in manifest["batches"]] == [1, 2, 0]  # per-batch error tally
    assert offline.status(res["job_id"])["job"]["rows_err"] == 3


def test_an_input_missing_a_signature_column_is_refused_up_front(tmp_path, registry):
    _frame(20).drop(columns=["mem"]).to_parquet(tmp_path / "in.parquet")
    out = offline.run(_spec(tmp_path))
    assert out["ok"] is False and out["code"] == "input_schema_mismatch" and "mem" in out["error"]
    assert offline.status(out["job_id"])["job"]["state"] == "failed"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"input": {"type": "local", "path": "/nonexistent/x.parquet"}}, "input_not_found"),
        ({"version": "99"}, "model_not_found"),
        ({"version": None, "alias": "NoSuchAlias"}, "model_not_found"),
    ],
)
def test_missing_inputs_and_models_are_refusals(tmp_path, registry, over, code):
    out = offline.run(_spec(tmp_path, **over))
    assert out["ok"] is False and out["code"] == code


def test_an_empty_directory_is_input_not_found(tmp_path, registry):
    (tmp_path / "empty").mkdir()
    out = offline.run(_spec(tmp_path, input={"type": "local", "path": str(tmp_path / "empty")}))
    assert out["code"] == "input_not_found"


def test_a_model_that_cannot_load_fails_the_job_visibly(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 20)

    def broken(model, version):
        raise OSError("artifact store unreachable")

    out = offline.run(_spec(tmp_path), loader=broken)
    assert out["ok"] is False and out["code"] == "model_load_failed"
    assert "unreachable" in out["error"] and out["state"] == "failed"


# ── dataplane in and out ───────────────────────────────────────────────────────────────────
def test_dataplane_snapshot_in_and_out(tmp_path, registry, monkeypatch):
    from examlops.dataplane import materialize, resolve, source_key, store_from_env
    from examlops.dataplane.store import publish

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", (tmp_path / "store").as_uri())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("EXAMLOPS_OFFLINE_WORKDIR", str(tmp_path / "work"))
    df = _frame(120)
    stage = tmp_path / "stage" / "jobs"
    stage.mkdir(parents=True)
    df.to_parquet(stage / "part-00000.parquet")
    st = store_from_env()
    key = source_key("", "pm100")
    snap, changed = publish(
        st,
        key,
        staged=[("jobs/part-00000.parquet", stage / "part-00000.parquet")],
        parent=None,
        connector="test",
        connection=None,
        spec_hash="s",
        watermark={},
        pull_id="p1",
        incremental=False,
    )
    assert changed

    spec = OfflineJob.load(
        {
            "model": MODEL,
            "version": "2",
            "input": {
                "type": "dataplane",
                "source": "pm100",
                "table": "jobs",
                "revision": "latest",
            },
            "output": {"type": "dataplane", "source": "scores"},
            "batch_size": 50,
            "idempotency_key": "dp-1",
        }
    )
    res = offline.run(spec)
    assert res["ok"], res
    assert res["input"]["revision"] == snap.revision  # `latest` was pinned to the exact revision
    assert res["output"]["type"] == "dataplane" and res["output"]["source"] == "_global/scores"
    assert (
        json.loads(offline_store.get(res["job_id"])["spec_json"])["input_revision"] == snap.revision
    )

    # The output is a real snapshot: resolvable by its revision, checksum-verified on download,
    # and its manifest carries the lineage of the run.
    ref = resolve(st, "_global/scores", res["output"]["revision"])
    local = materialize(st, ref, tmp_path / "cache2")
    got = pd.concat([pd.read_parquet(p) for p in sorted((local / "predictions").glob("*.parquet"))])
    real = mlflow.pyfunc.load_model(f"models:/{MODEL}/2")
    np.testing.assert_allclose(got["prediction"].to_numpy(), real.predict(df[COLUMNS]))
    om = json.loads(st.read_text(f"_global/scores/{res['job_id']}/_offline_manifest.json"))
    assert om["input"]["revision"] == snap.revision and om["model"]["version"] == "2"
    assert not (tmp_path / "work" / res["job_id"]).exists()

    # pinning an unknown revision is a refusal
    bad = OfflineJob.load(
        {
            **spec.to_dict(),
            "idempotency_key": "dp-2",
            "input": {**spec.to_dict()["input"], "revision": "f" * 64},
        }
    )
    assert offline.run(bad)["code"] == "input_not_found"
    wrong_table = OfflineJob.load(
        {
            **spec.to_dict(),
            "idempotency_key": "dp-3",
            "input": {**spec.to_dict()["input"], "table": "nope"},
        }
    )
    assert offline.run(wrong_table)["code"] == "input_not_found"


# ── cost, lineage, audit ───────────────────────────────────────────────────────────────────
def test_cost_is_recorded_when_resources_are_declared_and_honest_when_not(tmp_path, registry):
    from examlops.data import finops

    _write_input(tmp_path / "in.parquet", 120)
    none = offline.run(_spec(tmp_path, key="c0"))
    assert none["cost"]["metered"] is False and "nothing recorded" in none["cost"]["basis"]
    assert finops.get_model_costs(MODEL) == []

    res = offline.run(
        _spec(tmp_path, key="c1", resources={"cpus": 4, "gpus": 1}),
        loader=None,
    )
    cost = res["cost"]
    assert cost["metered"] is False and "declared resources" in cost["basis"]
    assert cost["gpu_hours"] > 0 and cost["cpu_hours"] > 0 and cost["items"] == 120
    rows = finops.get_model_costs(MODEL)
    assert len(rows) == 1 and rows[0]["version"] == 2 and rows[0]["job_id"] == res["job_id"]
    assert rows[0]["gpu_hours"] == pytest.approx(cost["gpu_hours"], abs=1e-6)
    if cost.get("cost_usd") is not None:  # priced by the cost provider: per-item cost is derived
        assert cost["cost_usd_per_item"] == pytest.approx(cost["cost_usd"] / 120)


def test_lineage_links_input_revision_and_model_to_the_output(tmp_path, registry):
    from examlops.data.events import lineage_impact

    _write_input(tmp_path / "in.parquet", 60)
    res = offline.run(_spec(tmp_path))
    assert res["lineage"] == {"emitted": True}
    from examlops.platform_db import get_db

    with get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lineage_events WHERE run_id=?", (res["job_id"],)
            ).fetchall()
        ]
    assert len(rows) == 1 and rows[0]["event_type"] == "COMPLETE"
    assert rows[0]["model"] == MODEL and rows[0]["model_version"] == "2"
    assert rows[0]["dataset_revision"] == res["output"]["revision"]
    with get_db() as conn:
        io = [
            (r["direction"], r["node_name"])
            for r in conn.execute(
                "SELECT direction, node_name FROM lineage_io WHERE run_id=?", (res["job_id"],)
            ).fetchall()
        ]
    ins = [n for d, n in io if d == "input"]
    outs = [n for d, n in io if d == "output"]
    assert any(res["input"]["revision"] in n for n in ins) and any(MODEL in n for n in ins)
    assert len(outs) == 1 and res["output"]["revision"] in outs[0]
    facets = json.loads(rows[0]["facets_json"])
    assert "mlflow-pyfunc-oip" in json.dumps(facets) and "workload_kind" in json.dumps(facets)
    assert lineage_impact(res["output"]["revision"])  # `exa models lineage --impact` sees it


def test_runs_are_audited(tmp_path, registry):
    from examlops.platform_db import get_db

    _write_input(tmp_path / "in.parquet", 30)
    res = offline.run(_spec(tmp_path), actor="alice")
    with get_db() as conn:
        events = [
            dict(r)
            for r in conn.execute("SELECT * FROM audit_events WHERE source='offline'").fetchall()
        ]
    assert [e["action"] for e in events] == ["offline_completed"]
    assert events[0]["actor"] == "alice" and events[0]["target"] == res["job_id"]


def _break_audit(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    monkeypatch.setattr(audit_mod, "append_audit_event", boom, raising=False)


def test_a_lost_offline_run_audit_is_counted_and_the_run_still_completes(
    tmp_path, registry, monkeypatch
):
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    _write_input(tmp_path / "in.parquet", 30)
    reset_dropped_audit_events()
    _break_audit(monkeypatch)
    res = offline.run(_spec(tmp_path))
    assert res["ok"] is True  # the audit is best-effort: the run is not undone by it
    assert dropped_audit_events().get("offline_completed") == 1
    reset_dropped_audit_events()


def test_a_lost_offline_cancel_audit_is_counted(tmp_path, registry, monkeypatch):
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    _write_input(tmp_path / "in.parquet", 250)
    job_id = offline.job_id_for("default", "k1")

    def cancel_at_first(info):
        if info["idx"] == 0:
            reset_dropped_audit_events()
            _break_audit(monkeypatch)
            assert offline.cancel(job_id)["ok"] is True  # requested despite the audit loss
            assert dropped_audit_events().get("offline_cancel_requested") == 1

    offline.run(_spec(tmp_path), on_batch=cancel_at_first)
    reset_dropped_audit_events()


# ── the operation handle ───────────────────────────────────────────────────────────────────
def test_ops_status_and_cancel_work_for_an_offline_job(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 60)
    res = offline.run(_spec(tmp_path))
    out = operations.status(res["job_id"])
    op = out["operation"]
    assert out["ok"] and op["kind"] == "offline_inference" and op["state"] == "completed"
    assert op["terminal"] is True and op["cancellable"] is False
    assert operations.wait(res["job_id"], timeout=0)["timed_out"] is False
    assert operations.status("off-nope")["code"] == "not_found"
    cancel = operations.cancel(res["job_id"])
    assert cancel["ok"] is False and cancel["code"] == "not_cancellable"


# ── the CLI ────────────────────────────────────────────────────────────────────────────────
def _exa(*args: str):
    from examlops.cli.main import app

    return CliRunner().invoke(app, list(args))


def test_cli_run_status_list_and_ops(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 120)
    args = (
        "offline",
        "run",
        "--model",
        MODEL,
        "--version",
        "2",
        "--input",
        str(tmp_path / "in.parquet"),
        "--output",
        str(tmp_path / "out"),
        "--key",
        "cli-1",
        "--batch-size",
        "50",
    )
    r = _exa("--json", *args)
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["state"] == "completed" and doc["counts"]["rows_ok"] == 120
    job_id = doc["job_id"]

    again = json.loads(_exa("--json", *args).stdout)
    assert again["replayed"] is True

    s = _exa("--json", "offline", "status", job_id)
    assert s.exit_code == 0 and json.loads(s.stdout)["state"] == "completed"
    listed = json.loads(_exa("--json", "offline", "list").stdout)
    assert [j["job_id"] for j in listed] == [job_id]
    assert json.loads(_exa("--json", "offline", "list", "--state", "failed").stdout) == []
    ops = _exa("--json", "ops", "status", job_id)  # ADR 0147: an offline job is an operation
    assert ops.exit_code == 0 and json.loads(ops.stdout)["state"] == "completed"

    human = _exa("offline", "run", *args[2:])
    assert human.exit_code == 0 and "replayed" in human.output


def test_cli_refusals_have_exit_code_1_and_one_json_document(tmp_path, registry):
    missing_key = _exa(
        "--json",
        "offline",
        "run",
        "--model",
        MODEL,
        "--version",
        "2",
        "--input",
        str(tmp_path),
        "--output",
        str(tmp_path / "o"),
    )
    assert missing_key.exit_code == 1 and json.loads(missing_key.stdout)["code"] == "invalid_spec"
    gen = _exa(
        "--json",
        "offline",
        "run",
        "--model",
        MODEL,
        "--version",
        "2",
        "--kind",
        "generative",
        "--input",
        str(tmp_path),
        "--output",
        str(tmp_path / "o"),
        "--key",
        "g",
    )
    assert gen.exit_code == 1 and json.loads(gen.stdout)["code"] == "kind_not_supported_offline"
    nf = _exa("--json", "offline", "status", "off-ghost")
    assert nf.exit_code == 1 and json.loads(nf.stdout)["code"] == "not_found"
    no_file = _exa("--json", "offline", "run", "--spec", str(tmp_path / "missing.json"))
    assert no_file.exit_code == 1


def test_cli_spec_file_and_fail_on_errors(tmp_path, registry):
    df = _frame(60)
    df.loc[3, "cpu"] = None
    df.to_parquet(tmp_path / "in.parquet")
    spec = _spec(tmp_path, key="spec-1", batch_size=60).to_dict()
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    ok = _exa("--json", "offline", "run", "--spec", str(tmp_path / "spec.json"))
    assert ok.exit_code == 0 and json.loads(ok.stdout)["counts"]["rows_err"] == 1
    spec["idempotency_key"] = "spec-2"
    (tmp_path / "spec2.json").write_text(json.dumps(spec))
    strict = _exa(
        "--json", "offline", "run", "--spec", str(tmp_path / "spec2.json"), "--fail-on-errors"
    )
    assert strict.exit_code == 1


def test_cli_cancel(tmp_path, registry):
    _write_input(tmp_path / "in.parquet", 30)
    res = offline.run(_spec(tmp_path))
    done = _exa("--json", "--yes", "offline", "cancel", res["job_id"])
    assert done.exit_code == 1 and json.loads(done.stdout)["code"] == "not_cancellable"


def test_the_environment_is_left_as_found():
    assert "EXAMLOPS_OFFLINE_LEASE_TTL" not in os.environ
