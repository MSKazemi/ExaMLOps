"""Per-model input schemas ride in the serving snapshot and are enforced on the replica (ADR 0123 d3).

Everything runs through the real code paths. The ``MLmodel`` text is the one MLflow itself writes
for a real sklearn model (so the parser is tested against MLflow's format, not ours), the compiler
and publisher run against a fake MLflow REST client, the snapshot is digest-verified by the replica's
own ``verified``, and requests go through the real ``MultiModelServer.predict`` / ``v2_infer`` /
``v2_model_metadata`` — with the platform database made unreachable to prove the reply path reads
nothing.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import HTTPException

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from examlops import serving_schema, serving_snapshot  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402
from serving.ray_serving import app as rs_app  # noqa: E402
from serving.ray_serving import oip  # noqa: E402
from serving.ray_serving.snapshot import verified  # noqa: E402

pd = pytest.importorskip("pandas")
sklearn_linear = pytest.importorskip("sklearn.linear_model")
mlflow = pytest.importorskip("mlflow")

COLUMNS = ["cpu", "mem", "nodes"]


def _save(tmp_path: Path, columns: list[str], name: str = "model"):
    """A real sklearn model with a real MLflow column signature: (loaded model, MLmodel text)."""
    from mlflow.models import infer_signature

    X = pd.DataFrame(np.random.default_rng(0).random((20, len(columns))), columns=columns)
    y = X.sum(axis=1)
    model = sklearn_linear.LinearRegression().fit(X, y)
    path = tmp_path / name
    mlflow.sklearn.save_model(model, str(path), signature=infer_signature(X, y))
    return mlflow.pyfunc.load_model(str(path)), (path / "MLmodel").read_text()


@pytest.fixture(scope="module")
def model3(tmp_path_factory):
    return _save(tmp_path_factory.mktemp("s3"), COLUMNS)


@pytest.fixture(scope="module")
def model2(tmp_path_factory):
    return _save(tmp_path_factory.mktemp("s2"), ["cpu", "mem"])


class _Resp:
    def __init__(self, body=None, status: int = 200, text: str = "") -> None:
        self._body, self.status_code, self.text = body or {}, status, text

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Mlflow:
    """Registered models + the tracking server's artifact route, for the compiler."""

    def __init__(self) -> None:
        self.aliases: dict[str, dict[str, str]] = {"jpcp": {"Production": "1"}}
        # (name, version) -> MLmodel text | None (artifact absent, 404) | Exception (store down)
        self.mlmodel: dict[tuple[str, str], object] = {}
        self.artifact_gets: list[dict] = []

    def get(self, url: str, params: dict | None = None) -> _Resp:
        params = params or {}
        if url.endswith("/get-artifact"):
            self.artifact_gets.append(dict(params))
            name, version = params["run_uuid"].split("-v")  # run ids are "<name>-v<version>"
            found = self.mlmodel.get((name, version))
            if isinstance(found, Exception):
                raise found
            return _Resp(status=404) if found is None else _Resp(text=str(found))
        if url.endswith("registered-models/search"):
            return _Resp(
                {
                    "registered_models": [
                        {
                            "name": n,
                            "aliases": [{"alias": a, "version": v} for a, v in per.items()],
                        }
                        for n, per in self.aliases.items()
                    ]
                }
            )
        name, version = params["name"], params["version"]
        return _Resp(
            {
                "model_version": {
                    "name": name,
                    "version": version,
                    "run_id": f"{name}-v{version}",
                    "source": f"s3://mlflow/1/{name}-v{version}/artifacts/model",
                    "tags": [],
                }
            }
        )


@pytest.fixture(autouse=True)
def _fresh():
    serving_snapshot._version_cache.clear()
    serving_snapshot._schema_cache.clear()
    yield
    serving_snapshot._version_cache.clear()
    serving_snapshot._schema_cache.clear()


def _compile(fake: _Mlflow) -> dict:
    return serving_snapshot.compile_snapshot(client=fake, mlflow_url="http://m")


def _schema_of(snapshot: dict, alias: str = "Production") -> dict | None:
    return snapshot["models"]["jpcp"]["aliases"][alias].get("input_schema")


# ─── the parser, against MLflow's own MLmodel ─────────────────────────────────


def test_a_real_mlmodel_signature_is_parsed(model3):
    schema = serving_schema.parse_mlmodel(model3[1])
    assert schema == {
        "kind": "columns",
        "inputs": [{"name": n, "type": "FP64"} for n in COLUMNS],
        "width": 3,
    }
    assert serving_schema.normalize(schema) == schema


def test_a_tensor_signature_is_parsed():
    text = (
        "signature:\n"
        '  inputs: \'[{"type": "tensor", "tensor-spec": {"dtype": "float32", "shape": [-1, 4]}}]\'\n'
    )
    assert serving_schema.parse_mlmodel(text) == {
        "kind": "tensors",
        "inputs": [{"name": "input-0", "type": "FP32"}],
        "width": 4,
    }


@pytest.mark.parametrize(
    "text",
    ["", "flavors: {}\n", "signature: {}\n", ":::not yaml", "signature:\n  inputs: '[1, 2]'\n"],
)
def test_no_signature_or_an_unreadable_one_is_no_schema(text):
    assert serving_schema.parse_mlmodel(text) is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"kind": "rows", "inputs": [{"name": "a", "type": "FP64"}]},
        {"kind": "columns", "inputs": []},
        {"kind": "columns", "inputs": [{"name": "a", "type": "FLOAT"}]},
        {"kind": "columns", "inputs": [{"name": "a", "type": "FP64"}] * 2},
        {"kind": "columns", "inputs": [{"name": "a", "type": "FP64"}], "width": 0},
    ],
)
def test_a_malformed_schema_normalizes_to_none(bad):
    assert serving_schema.normalize(bad) is None
    assert oip.signature_from_schema(bad) is None


def test_the_mlmodel_location_for_each_kind_of_source():
    run = serving_schema.mlmodel_location("s3://b/1/r1/artifacts/model", "r1")
    assert run == ("/get-artifact", {"run_uuid": "r1", "path": "model/MLmodel"})
    logged = serving_schema.mlmodel_location("models:/m-0a1b", None)
    assert logged and logged[0].endswith("/logged-models/m-0a1b/artifacts/files")
    assert serving_schema.mlmodel_location("s3://mlflow/jpcp/1", "r1") is None
    assert serving_schema.mlmodel_location(None, None) is None


# ─── compiling ────────────────────────────────────────────────────────────────


def test_the_compiler_puts_the_signature_in_the_alias_entry(model3):
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = model3[1]
    snap = _compile(fake)

    assert _schema_of(snap)["inputs"] == [{"name": n, "type": "FP64"} for n in COLUMNS]
    assert fake.artifact_gets == [{"run_uuid": "jpcp-v1", "path": "model/MLmodel"}]


def test_a_version_without_a_signature_has_no_key_at_all():
    """Absent is absent, not null: such a model hashes exactly as it did before schemas existed."""
    fake = _Mlflow()  # the artifact route answers 404
    snap = _compile(fake)
    assert "input_schema" not in snap["models"]["jpcp"]["aliases"]["Production"]
    assert set(snap["models"]["jpcp"]["aliases"]["Production"]) == {
        "version",
        "run_id",
        "source",
        "framework",
        "signature",
    }


def test_a_version_is_read_once_including_when_it_has_no_schema(model3):
    fake = _Mlflow()
    fake.aliases["mack"] = {"Production": "1"}
    fake.mlmodel[("jpcp", "1")] = model3[1]
    _compile(fake)
    _compile(fake)
    assert len(fake.artifact_gets) == 2  # jpcp v1 and mack v1, once each (404 is cached too)


def test_a_new_signature_bumps_the_generation_and_identical_content_does_not(model3, model2):
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = model3[1]
    fake.mlmodel[("jpcp", "2")] = model2[1]
    g1, published = serving_snapshot.publish(_compile(fake))
    assert published
    assert serving_snapshot.publish(_compile(fake)) == (g1, False)  # unchanged: no new generation

    fake.aliases["jpcp"]["Production"] = "2"  # promotion to a version with a different schema
    g2, published = serving_snapshot.publish(_compile(fake))
    assert published and g2 == g1 + 1
    assert [i["name"] for i in _schema_of(serving_snapshot.latest())["inputs"]] == ["cpu", "mem"]


def test_a_failed_read_keeps_the_previous_schema_and_is_not_cached(model3):
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = model3[1]
    g1, _ = serving_snapshot.publish(_compile(fake))

    serving_snapshot._schema_cache.clear()  # a compiler process restarted during the outage
    fake.mlmodel[("jpcp", "1")] = ConnectionError("artifact store down")
    during = _compile(fake)
    assert _schema_of(during) == _schema_of(serving_snapshot.latest())  # the check is not lifted
    assert serving_snapshot.publish(during) == (g1, False)  # ...and no spurious generation

    fake.mlmodel[("jpcp", "1")] = model3[1]  # the store is back: read again, not pinned as "none"
    assert _schema_of(_compile(fake)) is not None


def test_a_failed_read_with_no_previous_schema_is_unchecked_not_a_compile_failure(model3):
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = ConnectionError("artifact store down")
    snap = _compile(fake)  # alias moves must still reach serving while the store is down
    assert _schema_of(snap) is None
    assert serving_snapshot._schema_cache == {}


def test_schema_compilation_can_be_switched_off(monkeypatch, model3):
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = model3[1]
    monkeypatch.setenv("EXAMLOPS_SNAPSHOT_INPUT_SCHEMAS", "0")
    assert _schema_of(_compile(fake)) is None
    assert fake.artifact_gets == []


# ─── the digest ───────────────────────────────────────────────────────────────


def _published(model3) -> dict:
    fake = _Mlflow()
    fake.mlmodel[("jpcp", "1")] = model3[1]
    serving_snapshot.publish(_compile(fake))
    return serving_snapshot.latest()


def test_the_replica_accepts_a_published_snapshot_and_refuses_a_tampered_schema(model3):
    snap = _published(model3)
    assert verified(snap) is not None

    snap["models"]["jpcp"]["aliases"]["Production"]["input_schema"]["inputs"][0]["type"] = "BYTES"
    assert verified(snap) is None  # the schema is inside the digest

    gone = _published(model3)
    del gone["models"]["jpcp"]["aliases"]["Production"]["input_schema"]
    assert verified(gone) is None  # a section cannot be silently dropped either


def test_a_tampered_stored_row_is_refused_by_the_replica_reader(model3):
    _published(model3)
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT generation, body FROM serving_snapshots").fetchone()
        body = json.loads(row["body"])
        body["models"]["jpcp"]["aliases"]["Production"]["input_schema"]["width"] = 99
        conn.execute(
            "UPDATE serving_snapshots SET body=? WHERE generation=?",
            (json.dumps(body), row["generation"]),
        )
        conn.commit()
    assert verified(serving_snapshot.latest()) is None


def test_snapshots_from_before_schemas_and_before_quotas_still_verify():
    models = {"jpcp": {"name": "jpcp", "aliases": {"Production": {"version": "1"}}}}
    for content in (
        {"models": models, "traffic": {}, "shadow": {}},  # before quotas
        {
            "models": models,
            "traffic": {},
            "shadow": {},
            "quotas": {"tenants": {}},
        },  # before schemas
    ):
        old = {
            "schema": 1,
            "generation": 3,
            "digest": serving_snapshot.digest_of(content),
            **content,
        }
        assert verified(old) is not None


# ─── the replica ──────────────────────────────────────────────────────────────


def _server(loaded):
    srv = object.__new__(rs_app.MultiModelServer.func_or_class)
    srv._cache_lock = threading.RLock()
    srv._hot = {}
    srv._version_cache = OrderedDict()
    srv._version_cache_size = 8
    srv._preload_aliases = ["Production"]
    srv._replica_id = "t"
    srv._snapshot_reader = None
    srv._snapshot_generation = None
    srv._snapshot_shadow = None
    srv._shadow_cache = {}
    for attr in (
        "_req_counter",
        "_latency_hist",
        "_pred_value_hist",
        "_version_gauge",
        "_models_gauge",
        "_snapshot_gauge",
        "_reload_counter",
    ):
        setattr(srv, attr, MagicMock())
    srv._predict_pool = ThreadPoolExecutor(max_workers=2)
    srv._predict_timeout = 30.0
    srv._mirror = lambda *a, **k: None
    srv._load_by_flavour = lambda name, alias, mv: loaded[str(mv.version)]
    return srv


@pytest.fixture()
def replica(model3, model2, monkeypatch):
    monkeypatch.setattr(rs_app, "_get_serve_aliases_for", lambda name: ["Production"])
    srv = _server({"1": model3[0], "2": model2[0]})
    yield srv
    srv._predict_pool.shutdown(wait=False)


def _apply(srv, model3, generation: int = 1, mutate=None) -> dict:
    snap = _published(model3)
    snap["generation"] = generation
    if mutate:
        mutate(snap)
        snap["digest"] = serving_snapshot.digest_of(
            {k: snap[k] for k in serving_snapshot.CONTENT_KEYS if k in snap}
        )
    assert verified(snap) is not None
    srv._apply_snapshot(snap)
    return snap


def _no_datastore(monkeypatch):
    """Any read of the platform database from here on fails the test: the reply path reads none."""

    def boom(*a, **k):
        raise AssertionError("the reply path read the platform database")

    monkeypatch.setattr("examlops.platform_db.get_db", boom)
    monkeypatch.setattr("examlops.serving_snapshot.latest", boom)
    monkeypatch.setattr("examlops.serving_snapshot.latest_generation", boom)


GOOD = {"cpu": 1.0, "mem": 2.0, "nodes": 3.0}


def test_a_valid_request_is_served_from_the_snapshot_without_a_database_read(
    replica, model3, monkeypatch
):
    _apply(replica, model3)
    _no_datastore(monkeypatch)
    answer = replica.predict("jpcp", rs_app.PredictRequest(features=GOOD))
    assert np.isclose(answer.prediction, 6.0, atol=1e-6)
    assert replica._hot["jpcp", "Production"]["input_schema"]["width"] == 3


def test_a_missing_field_is_422_and_names_it(replica, model3):
    _apply(replica, model3)
    with pytest.raises(HTTPException) as err:
        replica.predict("jpcp", rs_app.PredictRequest(features={"cpu": 1.0, "mem": 2.0}))
    assert err.value.status_code == 422
    assert "nodes" in str(err.value.detail)
    assert replica._req_counter.inc.call_args.kwargs["tags"]["status"] == "invalid"


@pytest.mark.parametrize("bad", ["fast", None, float("nan"), [1.0]])
def test_a_field_of_the_wrong_type_is_422_and_names_it(replica, model3, bad):
    _apply(replica, model3)
    with pytest.raises(HTTPException) as err:
        replica.predict("jpcp", rs_app.PredictRequest(features={**GOOD, "mem": bad}))
    assert err.value.status_code == 422
    assert "'mem'" in str(err.value.detail)


def test_extra_fields_are_ignored_as_predict_always_did(replica, model3):
    _apply(replica, model3)
    answer = replica.predict("jpcp", rs_app.PredictRequest(features={**GOOD, "note": "hi"}))
    assert np.isclose(answer.prediction, 6.0, atol=1e-6)


def test_without_a_schema_in_the_snapshot_behaviour_is_unchanged(replica, model3):
    """The model's own signature still decides, exactly as before this feature existed."""

    def drop(snap):
        del snap["models"]["jpcp"]["aliases"]["Production"]["input_schema"]

    _apply(replica, model3, mutate=drop)
    assert replica._hot["jpcp", "Production"]["input_schema"] is None
    # a numeric string was always accepted by /predict's own float() conversion
    answer = replica.predict("jpcp", rs_app.PredictRequest(features={**GOOD, "mem": "2.0"}))
    assert np.isclose(answer.prediction, 6.0, atol=1e-6)
    with pytest.raises(HTTPException) as err:  # and the model's signature still names a gap
        replica.predict("jpcp", rs_app.PredictRequest(features={"cpu": 1.0}))
    assert err.value.status_code == 422


def test_a_malformed_schema_fails_open(replica, model3):
    def rot(snap):
        snap["models"]["jpcp"]["aliases"]["Production"]["input_schema"] = {"kind": "rows"}

    _apply(replica, model3, mutate=rot)
    answer = replica.predict("jpcp", rs_app.PredictRequest(features={**GOOD, "mem": "2.0"}))
    assert np.isclose(answer.prediction, 6.0, atol=1e-6)


def test_enforcement_can_be_switched_off(replica, model3, monkeypatch):
    _apply(replica, model3)
    monkeypatch.setenv("RAY_INPUT_SCHEMA", "off")
    answer = replica.predict("jpcp", rs_app.PredictRequest(features={**GOOD, "mem": "2.0"}))
    assert np.isclose(answer.prediction, 6.0, atol=1e-6)


def test_a_new_generation_with_the_same_version_updates_the_schema(replica, model3):
    _apply(replica, model3, generation=1)

    def widen(snap):
        snap["models"]["jpcp"]["aliases"]["Production"]["input_schema"]["inputs"][2]["name"] = "n"

    _apply(replica, model3, generation=2, mutate=widen)  # same version 1: not reloaded
    names = [i["name"] for i in replica._hot["jpcp", "Production"]["input_schema"]["inputs"]]
    assert names == ["cpu", "mem", "n"]


def test_oip_metadata_is_served_from_the_snapshot_schema(replica, model3):
    def retype(snap):
        snap["models"]["jpcp"]["aliases"]["Production"]["input_schema"]["inputs"][0]["type"] = (
            "FP32"
        )

    _apply(replica, model3, mutate=retype)
    meta = replica._v2_metadata("jpcp", None)
    assert [(i["name"], i["datatype"]) for i in meta["inputs"]] == [
        ("cpu", "FP32"),
        ("mem", "FP64"),
        ("nodes", "FP64"),
    ]  # the model itself says FP64 for all three: this came from the snapshot


def _tensors(**cols) -> dict:
    return {
        "inputs": [
            {"name": n, "shape": [1], "datatype": "FP64", "data": [v]} for n, v in cols.items()
        ]
    }


def test_oip_infer_validates_against_the_snapshot_schema_and_names_the_field(
    replica, model3, monkeypatch
):
    _apply(replica, model3)
    _no_datastore(monkeypatch)
    ok = replica._v2_infer("jpcp", None, _tensors(cpu=1.0, mem=2.0, nodes=3.0), None)
    assert np.isclose(ok["outputs"][0]["data"][0], 6.0, atol=1e-6)

    bad = replica._v2_infer("jpcp", None, _tensors(cpu=1.0, mem=2.0), None)
    assert bad.status_code == 400 and "nodes" in json.loads(bad.body)["error"]
    assert replica._req_counter.inc.call_args.kwargs["tags"]["status"] == "invalid"
