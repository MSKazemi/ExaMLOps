"""Open Inference Protocol v2 on the Ray model server: a conformance suite (plan P4.5, ADR 0141 d6).

Runs against a real MLflow sklearn model with a column signature, loaded through ``mlflow.pyfunc``
exactly as serving loads it, so metadata, name-to-column mapping and schema enforcement are MLflow's
own, not a mock's. Covers what the protocol requires — server and model metadata, health and
readiness (200 true / 4xx false, empty body), inference with shaped, typed tensors, and
``{"error": …}`` bodies — and what the platform adds: validation against the signature *before* the
model runs, version and alias selection, and the caller's time budget.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from serving.ray_serving import app as rs_app  # noqa: E402
from serving.ray_serving import oip  # noqa: E402

pd = pytest.importorskip("pandas")
sklearn_linear = pytest.importorskip("sklearn.linear_model")
mlflow = pytest.importorskip("mlflow")

COLUMNS = ["cpu", "mem", "nodes"]


@pytest.fixture(scope="module")
def pyfunc_model(tmp_path_factory):
    """y = 1·cpu + 2·mem + 3·nodes, saved with a column signature and loaded as serving loads it."""
    from mlflow.models import infer_signature

    X = pd.DataFrame(np.random.default_rng(0).random((20, 3)), columns=COLUMNS)
    y = X["cpu"] + 2 * X["mem"] + 3 * X["nodes"]
    model = sklearn_linear.LinearRegression().fit(X, y)
    path = tmp_path_factory.mktemp("oip") / "model"
    mlflow.sklearn.save_model(model, str(path), signature=infer_signature(X, y))
    return mlflow.pyfunc.load_model(str(path))


@pytest.fixture()
def server(pyfunc_model):
    srv = object.__new__(rs_app.MultiModelServer.func_or_class)
    srv._cache_lock = threading.RLock()
    srv._hot = {("jpcp", "Production"): {"model": pyfunc_model, "version": "3", "run_id": "r3"}}
    srv._version_cache = OrderedDict()
    srv._version_cache_size = 8
    srv._preload_aliases = list(rs_app.PRELOAD_ALIASES)
    for attr in ("_req_counter", "_latency_hist", "_pred_value_hist", "_version_gauge"):
        setattr(srv, attr, MagicMock())
    srv._predict_pool = ThreadPoolExecutor(max_workers=2)
    srv._predict_timeout = 30.0
    srv._mirror = lambda *a, **k: None
    yield srv
    srv._predict_pool.shutdown(wait=False)


def _body(response) -> dict:
    import json

    return json.loads(response.body) if hasattr(response, "body") else response


def _statuses(srv) -> list[str]:
    return [c.kwargs["tags"]["status"] for c in srv._req_counter.inc.call_args_list]


# ─── the route table ──────────────────────────────────────────────────────────


def test_every_protocol_route_is_served():
    paths = {(m, r.path) for r in rs_app._app.routes for m in getattr(r, "methods", [])}
    for route in (
        ("GET", "/v2"),
        ("GET", "/v2/health/live"),
        ("GET", "/v2/health/ready"),
        ("GET", "/v2/models/{model_name}"),
        ("GET", "/v2/models/{model_name}/versions/{version}"),
        ("GET", "/v2/models/{model_name}/ready"),
        ("GET", "/v2/models/{model_name}/versions/{version}/ready"),
        ("POST", "/v2/models/{model_name}/infer"),
        ("POST", "/v2/models/{model_name}/versions/{version}/infer"),
    ):
        assert route in paths, route


# ─── metadata and health ──────────────────────────────────────────────────────


def test_server_metadata_and_liveness(server):
    meta = server.v2_server_metadata()
    assert meta["name"] and meta["version"] and meta["extensions"] == []
    live = server.v2_health_live()
    assert live.status_code == 200 and live.body == b""


def test_readiness_is_200_true_and_4xx_false_with_an_empty_body(server):
    assert server.v2_health_ready().status_code == 200
    assert server.v2_model_ready("jpcp").status_code == 200
    assert server.v2_model_version_ready("jpcp", "3").status_code == 200
    assert server.v2_model_version_ready("jpcp", "9").status_code == 400
    server._hot = {}
    not_ready = server.v2_health_ready()
    assert not_ready.status_code == 400 and not_ready.body == b""


def test_model_metadata_comes_from_the_signature(server):
    meta = server.v2_model_metadata("jpcp")
    assert meta["name"] == "jpcp" and meta["versions"] == ["3"]
    assert [i["name"] for i in meta["inputs"]] == COLUMNS
    assert {i["datatype"] for i in meta["inputs"]} == {"FP64"}
    assert meta["outputs"][0]["name"] == "predict"


def test_an_unknown_model_is_an_error_document(server):
    response = server.v2_model_metadata("nope")
    assert response.status_code == 404 and "error" in _body(response)


# ─── inference ────────────────────────────────────────────────────────────────


def test_a_batch_of_rows_in_one_tensor(server, pyfunc_model):
    rows = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    body = {
        "id": "req-1",
        "inputs": [{"name": "input-0", "shape": [3, 3], "datatype": "FP64", "data": rows}],
    }
    answer = server.v2_infer("jpcp", body=body, budget_ms=None)
    assert answer["model_name"] == "jpcp" and answer["model_version"] == "3"
    assert answer["id"] == "req-1"
    (output,) = answer["outputs"]
    assert output["name"] == "predict" and output["datatype"] == "FP64" and output["shape"] == [3]
    expected = pyfunc_model.predict(pd.DataFrame(rows, columns=COLUMNS))
    assert np.allclose(output["data"], expected)
    assert np.allclose(output["data"], [1.0, 2.0, 3.0], atol=1e-6)


def test_named_columns_are_matched_by_name_not_by_position(server):
    """Sent in a different order than the signature; each value still reaches its own column."""
    body = {
        "inputs": [
            {"name": "nodes", "shape": [2], "datatype": "FP32", "data": [1, 0]},
            {"name": "cpu", "shape": [2], "datatype": "INT64", "data": [0, 1]},
            {"name": "mem", "shape": [2], "datatype": "FP64", "data": [0.0, 0.0]},
        ]
    }
    answer = server.v2_infer("jpcp", body=body, budget_ms=None)
    assert np.allclose(answer["outputs"][0]["data"], [3.0, 1.0], atol=1e-6)


@pytest.mark.parametrize(
    ("inputs", "fragment"),
    [
        (
            [{"name": "cpu", "shape": [1], "datatype": "FP64", "data": [1.0]}],
            "missing ['mem', 'nodes']",
        ),
        (
            [{"name": "input-0", "shape": [1, 2], "datatype": "FP64", "data": [1.0, 2.0]}],
            "takes 3 features per row; the request has 2",
        ),
        (
            [{"name": "input-0", "shape": [2, 3], "datatype": "FP64", "data": [1.0, 2.0]}],
            "holds 6 values, data has 2",
        ),
        (
            [{"name": "input-0", "shape": [1, 3], "datatype": "BYTES", "data": ["a", "b", "c"]}],
            "BYTES is not a numeric feature",
        ),
        (
            [{"name": "input-0", "shape": [1, 3], "datatype": "FP99", "data": [1, 2, 3]}],
            "unknown datatype",
        ),
        ([], "non-empty list"),
    ],
)
def test_invalid_requests_are_refused_before_the_model_runs(server, inputs, fragment):
    server._hot["jpcp", "Production"]["model"] = spy = MagicMock(
        wraps=server._hot["jpcp", "Production"]["model"]
    )
    response = server.v2_infer("jpcp", body={"inputs": inputs}, budget_ms=None)
    assert response.status_code == 400
    assert fragment in _body(response)["error"]
    spy.predict.assert_not_called()
    assert _statuses(server) == ["invalid"]


def test_the_version_path_serves_that_version(server, monkeypatch):
    seen: dict = {}
    real = server._resolve

    def resolve(name, alias, version):
        seen.update(alias=alias, version=version)
        return real(name, "Production", None)

    monkeypatch.setattr(server, "_resolve", resolve)
    body = {
        "inputs": [{"name": "input-0", "shape": [3], "datatype": "FP64", "data": [1.0, 1.0, 1.0]}],
        "parameters": {"alias": "Canary"},  # a pinned version wins over an alias
    }
    server.v2_version_infer("jpcp", "3", body=body, budget_ms=None)
    assert seen == {"alias": None, "version": "3"}


def test_an_alias_is_selected_through_parameters(server, pyfunc_model):
    server._hot["jpcp", "Canary"] = {"model": pyfunc_model, "version": "4", "run_id": "r4"}
    body = {
        "inputs": [{"name": "input-0", "shape": [3], "datatype": "FP64", "data": [1.0, 1.0, 1.0]}],
        "parameters": {"alias": "Canary"},
        "outputs": [{"name": "runtime"}],
    }
    answer = server.v2_infer("jpcp", body=body, budget_ms=None)
    assert answer["model_version"] == "4"
    assert answer["parameters"] == {"alias": "Canary", "run_id": "r4"}
    assert answer["outputs"][0]["name"] == "runtime"


def test_a_spent_budget_is_refused_without_running_the_model(server):
    body = {"inputs": [{"name": "input-0", "shape": [3], "datatype": "FP64", "data": [1, 1, 1]}]}
    response = server.v2_infer("jpcp", body=body, budget_ms="0")
    assert response.status_code == 504 and "deadline" in _body(response)["error"]
    assert _statuses(server) == ["deadline_exceeded"]


def test_predict_and_v2_share_one_execution_path(server):
    """Same model, same row, same answer — and the same metrics — through either protocol."""
    legacy = server.predict(
        "jpcp", rs_app.PredictRequest(features={"cpu": 1.0, "mem": 1.0, "nodes": 1.0})
    )
    v2 = server.v2_infer(
        "jpcp",
        body={"inputs": [{"name": "input-0", "shape": [3], "datatype": "FP64", "data": [1, 1, 1]}]},
        budget_ms=None,
    )
    assert np.isclose(legacy.prediction, v2["outputs"][0]["data"][0])
    assert _statuses(server) == ["success", "success"]


# ─── the protocol module on its own ───────────────────────────────────────────


def test_integer_predictions_are_int64_and_scalars_become_shape_one():
    body = oip.response("m", "1", np.array([1, 0, 1]))
    assert body["outputs"][0]["datatype"] == "INT64" and body["outputs"][0]["shape"] == [3]
    assert oip.response("m", "1", 2.5)["outputs"][0]["shape"] == [1]


def test_a_model_without_a_signature_takes_rows_in_one_tensor():
    body = {"inputs": [{"name": "x", "shape": [2, 2], "datatype": "FP32", "data": [1, 2, 3, 4]}]}
    assert oip.to_array(body, None).tolist() == [[1.0, 2.0], [3.0, 4.0]]
    meta = oip.model_metadata("m", ["1"], "mlflow", None)
    assert meta["inputs"] == [{"name": "input-0", "datatype": "FP64", "shape": [-1, -1]}]


def test_predict_reads_a_signed_models_features_by_name(server):
    """/predict used to flatten the feature dict in its own order and hand MLflow an unnamed
    array, which a column signature refuses (500). Features now go to their columns by name."""
    answer = server.predict(
        "jpcp", rs_app.PredictRequest(features={"nodes": 1.0, "cpu": 0.0, "mem": 0.0})
    )
    assert np.isclose(answer.prediction, 3.0, atol=1e-6)  # nodes counts 3, not cpu's 1
    with pytest.raises(rs_app.HTTPException) as refused:
        server.predict("jpcp", rs_app.PredictRequest(features={"cpu": 1.0}))
    assert refused.value.status_code == 422 and "mem" in refused.value.detail


def test_predict_announces_its_successor(server):
    from fastapi import Response

    response = Response()
    server.predict(
        "jpcp",
        rs_app.PredictRequest(features={"cpu": 1.0, "mem": 1.0, "nodes": 1.0}),
        response=response,
    )
    assert response.headers["Deprecation"] == "@1789084800"
    assert response.headers["Link"] == '</v2/models/jpcp/infer>; rel="successor-version"'


def test_v2_answers_carry_the_run_id_callers_read(server):
    body = {"inputs": [{"name": "input-0", "shape": [3], "datatype": "FP64", "data": [1, 1, 1]}]}
    answer = server.v2_infer("jpcp", body=body, budget_ms=None)
    assert answer["parameters"] == {"alias": "Production", "run_id": "r3"}


@pytest.mark.parametrize(
    "answer",
    [
        {"not": "an OIP answer"},
        {"outputs": []},
        {"outputs": [{"name": "predict"}]},
        {"outputs": "x"},
    ],
)
def test_an_answer_without_outputs_is_refused_not_read_as_an_empty_prediction(answer):
    """It used to come back as `prediction: []`, a success nobody could tell from a real one."""
    from examlops import oip_client

    with pytest.raises(ValueError, match="Open Inference Protocol"):
        oip_client.result(answer)
