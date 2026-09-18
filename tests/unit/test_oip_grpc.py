"""The Open Inference Protocol v2 over gRPC (ADR 0126): the model server's gRPC front end.

`serving/oip_grpc/server.py` answers `inference.GRPCInferenceService` through the model server's
REST implementation of the same protocol. These tests hold:

- the stubs to the protocol's own proto (checksum, service, the field numbers clients depend on);
- the conversions both ways: typed and raw tensors, every datatype, parameters, flattening;
- the mapping of REST answers to gRPC status codes, and of a gRPC deadline to the request budget;
- that a model name is a path segment, so no gRPC request can reach another route;
- end to end: a real gRPC client, the real server, and the REST OIP methods of the model server with
  a real MLflow model behind them (the fixtures of test_oip_v2.py), so gRPC and REST give the same
  answers and the same errors.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import grpc
import httpx
import numpy as np
import pytest
from fastapi import Request

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from serving.oip_grpc import PROTO_SHA256  # noqa: E402
from serving.oip_grpc import open_inference_grpc_pb2 as pb  # noqa: E402
from serving.oip_grpc import open_inference_grpc_pb2_grpc as pb_grpc  # noqa: E402
from serving.oip_grpc import server as oip_grpc  # noqa: E402
from tests.unit.test_oip_v2 import COLUMNS, pyfunc_model, server  # noqa: E402, F401

PROTO = REPO / "serving" / "oip_grpc" / "open_inference_grpc.proto"


# ── the stubs are the protocol's ──────────────────────────────────────────────


def test_the_stubs_were_generated_from_the_committed_proto():
    digest = hashlib.sha256(PROTO.read_bytes()).hexdigest()
    assert digest == PROTO_SHA256, (
        "open_inference_grpc.proto changed: regenerate the stubs (see serving/oip_grpc/__init__.py) "
        "and update PROTO_SHA256"
    )
    assert "package inference;" in PROTO.read_text()


def test_the_service_and_the_field_numbers_clients_depend_on():
    service = pb.DESCRIPTOR.services_by_name["GRPCInferenceService"]
    assert service.full_name == "inference.GRPCInferenceService"
    assert {m.name for m in service.methods} == {
        "ServerLive", "ServerReady", "ModelReady", "ServerMetadata", "ModelMetadata", "ModelInfer",
    }  # fmt: skip
    numbers = {
        (m, f): pb.DESCRIPTOR.message_types_by_name[m].fields_by_name[f].number
        for m, f in [
            ("ModelInferRequest", "model_name"), ("ModelInferRequest", "inputs"),
            ("ModelInferRequest", "raw_input_contents"), ("ModelInferResponse", "outputs"),
            ("ModelInferResponse", "raw_output_contents"), ("InferTensorContents", "fp64_contents"),
            ("InferTensorContents", "bytes_contents"), ("InferParameter", "uint64_param"),
        ]
    }  # fmt: skip
    assert numbers == {
        ("ModelInferRequest", "model_name"): 1, ("ModelInferRequest", "inputs"): 5,
        ("ModelInferRequest", "raw_input_contents"): 7, ("ModelInferResponse", "outputs"): 5,
        ("ModelInferResponse", "raw_output_contents"): 6, ("InferTensorContents", "fp64_contents"): 7,
        ("InferTensorContents", "bytes_contents"): 8, ("InferParameter", "uint64_param"): 5,
    }  # fmt: skip


# ── conversions ───────────────────────────────────────────────────────────────


def _tensor(name, datatype, shape, **contents) -> pb.ModelInferRequest.InferInputTensor:
    return pb.ModelInferRequest.InferInputTensor(
        name=name, datatype=datatype, shape=shape, contents=pb.InferTensorContents(**contents)
    )


@pytest.mark.parametrize(
    ("datatype", "field", "values"),
    [
        ("BOOL", "bool_contents", [True, False]),
        ("INT8", "int_contents", [-3, 4]),
        ("INT32", "int_contents", [1, -2]),
        ("INT64", "int64_contents", [2**40, -1]),
        ("UINT8", "uint_contents", [7, 255]),
        ("UINT64", "uint64_contents", [2**63, 1]),
        ("FP32", "fp32_contents", [0.5, -1.25]),
        ("FP64", "fp64_contents", [1e-300, 2.0]),
    ],
)
def test_typed_inputs_become_the_rest_form(datatype, field, values):
    request = pb.ModelInferRequest(
        model_name="jpcp", id="r1", inputs=[_tensor("x", datatype, [2], **{field: values})]
    )
    body = oip_grpc.request_to_json(request)
    assert body == {
        "id": "r1",
        "inputs": [{"name": "x", "shape": [2], "datatype": datatype, "data": values}],
    }


def test_bytes_inputs_are_text():
    request = pb.ModelInferRequest(
        inputs=[_tensor("s", "BYTES", [2], bytes_contents=[b"a", "é".encode()])]
    )
    assert oip_grpc.request_to_json(request)["inputs"][0]["data"] == ["a", "é"]
    bad = pb.ModelInferRequest(inputs=[_tensor("s", "BYTES", [1], bytes_contents=[b"\xff"])])
    with pytest.raises(oip_grpc.BadRequest, match="not UTF-8"):
        oip_grpc.request_to_json(bad)


@pytest.mark.parametrize(
    ("datatype", "values", "dtype"),
    [
        ("FP32", [0.5, 1.5, -2.0, 4.0], "<f4"),
        ("FP16", [0.5, 1.5, -2.0, 4.0], "<f2"),  # raw only, as the protocol requires
        ("INT64", [1, -2, 3, 2**40], "<i8"),
        ("UINT16", [1, 2, 3, 65535], "<u2"),
        ("BOOL", [True, False, True, True], "?"),
    ],
)
def test_raw_inputs_are_decoded_little_endian(datatype, values, dtype):
    raw = np.array(values, dtype=dtype).tobytes()
    request = pb.ModelInferRequest(
        inputs=[pb.ModelInferRequest.InferInputTensor(name="x", datatype=datatype, shape=[2, 2])],
        raw_input_contents=[raw],
    )
    (entry,) = oip_grpc.request_to_json(request)["inputs"]
    assert entry["shape"] == [2, 2] and entry["data"] == values


def test_raw_bytes_elements_carry_a_length_prefix():
    raw = b"".join(struct.pack("<I", len(s)) + s for s in (b"ab", b"", b"xyz"))
    request = pb.ModelInferRequest(
        inputs=[pb.ModelInferRequest.InferInputTensor(name="s", datatype="BYTES", shape=[3])],
        raw_input_contents=[raw],
    )
    assert oip_grpc.request_to_json(request)["inputs"][0]["data"] == ["ab", "", "xyz"]


@pytest.mark.parametrize(
    ("request_", "fragment"),
    [
        (pb.ModelInferRequest(
            inputs=[pb.ModelInferRequest.InferInputTensor(name="x", datatype="FP32", shape=[3])],
            raw_input_contents=[b"\x00" * 8]), "needs 12"),
        (pb.ModelInferRequest(
            inputs=[pb.ModelInferRequest.InferInputTensor(name="x", datatype="FP32", shape=[1]),
                    pb.ModelInferRequest.InferInputTensor(name="y", datatype="FP32", shape=[1])],
            raw_input_contents=[b"\x00" * 4]), "1 entries for 2 inputs"),
        (pb.ModelInferRequest(
            inputs=[_tensor("x", "FP32", [1], fp32_contents=[1.0])],
            raw_input_contents=[b"\x00" * 4]), "both set"),
        (pb.ModelInferRequest(inputs=[_tensor("x", "FP16", [1])]), "raw_input_contents"),
        (pb.ModelInferRequest(
            inputs=[pb.ModelInferRequest.InferInputTensor(name="x", datatype="FP32", shape=[-1])],
            raw_input_contents=[b""]), "negative"),
        (pb.ModelInferRequest(
            inputs=[pb.ModelInferRequest.InferInputTensor(name="s", datatype="BYTES", shape=[1])],
            raw_input_contents=[struct.pack("<I", 9) + b"abc"]), "past the end"),
    ],
)  # fmt: skip
def test_malformed_requests_are_refused(request_, fragment):
    with pytest.raises(oip_grpc.BadRequest, match=fragment):
        oip_grpc.request_to_json(request_)


@pytest.mark.parametrize(
    ("value", "field"),
    [(True, "bool_param"), (7, "int64_param"), (2**64 - 1, "uint64_param"), (0.25, "double_param"),
     ("Canary", "string_param")],
)  # fmt: skip
def test_parameters_round_trip(value, field):
    param = oip_grpc.param_from_json(value)
    assert param.WhichOneof("parameter_choice") == field
    assert oip_grpc.param_to_json(param) == value


def test_request_parameters_ids_and_requested_outputs_reach_rest():
    request = pb.ModelInferRequest(
        id="abc", inputs=[_tensor("x", "FP64", [1], fp64_contents=[1.0])],
        outputs=[pb.ModelInferRequest.InferRequestedOutputTensor(name="predict")],
    )  # fmt: skip
    request.parameters["alias"].string_param = "Canary"
    body = oip_grpc.request_to_json(request)
    assert body["id"] == "abc" and body["parameters"] == {"alias": "Canary"}
    assert body["outputs"] == [{"name": "predict"}]


def test_answers_become_typed_outputs():
    answer = {
        "model_name": "jpcp", "model_version": "3", "id": "r1",
        "parameters": {"alias": "Production", "run_id": "r3", "cached": True},
        "outputs": [
            {"name": "predict", "datatype": "FP64", "shape": [2, 1], "data": [[1.5], [2.5]]},
            {"name": "label", "datatype": "BYTES", "shape": [2], "data": ["a", "b"]},
            {"name": "n", "datatype": "INT64", "data": [3]},
        ],
    }  # fmt: skip
    response = oip_grpc.response_from_json(answer)
    assert (response.model_name, response.model_version, response.id) == ("jpcp", "3", "r1")
    assert response.parameters["run_id"].string_param == "r3"
    assert response.parameters["cached"].bool_param is True
    predict, label, n = response.outputs
    assert list(predict.shape) == [2, 1] and list(predict.contents.fp64_contents) == [1.5, 2.5]
    assert list(label.contents.bytes_contents) == [b"a", b"b"]
    assert list(n.shape) == [1] and list(n.contents.int64_contents) == [3]


def test_an_output_that_cannot_be_sent_typed_is_an_error():
    with pytest.raises(ValueError, match="FP16"):
        oip_grpc.response_from_json({"outputs": [{"name": "h", "datatype": "FP16", "data": [1]}]})


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"),
     (404, "NOT_FOUND"), (429, "RESOURCE_EXHAUSTED"), (503, "UNAVAILABLE"),
     (504, "DEADLINE_EXCEEDED"), (500, "INTERNAL"), (502, "INTERNAL"), (418, "INVALID_ARGUMENT")],
)  # fmt: skip
def test_rest_statuses_map_to_grpc_codes(status, code):
    assert oip_grpc.status_for(status) == getattr(grpc.StatusCode, code)


# ── the running server ────────────────────────────────────────────────────────


def _rest_app(srv):
    """The model server's /v2 routes on its real methods, as Ray's ingress binds them."""
    from fastapi import FastAPI

    from serving.budgets import HEADER

    app = FastAPI()
    app.get("/v2")(lambda: srv.v2_server_metadata())
    app.get("/v2/health/live")(lambda: srv.v2_health_live())
    app.get("/v2/health/ready")(lambda: srv.v2_health_ready())
    app.get("/v2/models/{name}")(lambda name: srv.v2_model_metadata(name))
    app.get("/v2/models/{name}/versions/{version}")(
        lambda name, version: srv.v2_model_version_metadata(name, version)
    )
    app.get("/v2/models/{name}/ready")(lambda name: srv.v2_model_ready(name))
    app.get("/v2/models/{name}/versions/{version}/ready")(
        lambda name, version: srv.v2_model_version_ready(name, version)
    )

    async def infer(name: str, request: Request, version: str | None = None):
        body = await request.json()
        budget = request.headers.get(HEADER)
        if version is None:
            return srv.v2_infer(name, body=body, budget_ms=budget)
        return srv.v2_version_infer(name, version, body=body, budget_ms=budget)

    app.post("/v2/models/{name}/infer")(infer)
    app.post("/v2/models/{name}/versions/{version}/infer")(infer)
    return app


@pytest.fixture()
def grpc_stub(server):  # noqa: F811 - the model server fixture from test_oip_v2
    handle = oip_grpc.start_in_thread(
        "127.0.0.1", 0, "http://model-server", transport=httpx.ASGITransport(app=_rest_app(server))
    )
    channel = grpc.insecure_channel(f"127.0.0.1:{handle.port}")
    try:
        yield pb_grpc.GRPCInferenceServiceStub(channel), server
    finally:
        channel.close()
        handle.stop(grace=1)


def _rows(rows) -> pb.ModelInferRequest:
    flat = [v for row in rows for v in row]
    return pb.ModelInferRequest(
        model_name="jpcp",
        id="req-1",
        inputs=[_tensor("input-0", "FP64", [len(rows), 3], fp64_contents=flat)],
    )


def test_health_and_metadata_over_grpc(grpc_stub):
    stub, srv = grpc_stub
    assert stub.ServerLive(pb.ServerLiveRequest()).live is True
    assert stub.ServerReady(pb.ServerReadyRequest()).ready is True
    assert stub.ModelReady(pb.ModelReadyRequest(name="jpcp")).ready is True
    assert stub.ModelReady(pb.ModelReadyRequest(name="jpcp", version="9")).ready is False
    meta = stub.ServerMetadata(pb.ServerMetadataRequest())
    assert meta.name == srv.v2_server_metadata()["name"]
    model = stub.ModelMetadata(pb.ModelMetadataRequest(name="jpcp"))
    assert model.name == "jpcp" and list(model.versions) == ["3"]
    assert [i.name for i in model.inputs] == COLUMNS and model.outputs[0].name == "predict"


def test_inference_over_grpc_matches_rest(grpc_stub):
    stub, srv = grpc_stub
    rows = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    answer = stub.ModelInfer(_rows(rows))
    rest = srv.v2_infer(
        "jpcp",
        body={"id": "req-1", "inputs": [{"name": "input-0", "shape": [3, 3], "datatype": "FP64",
                                           "data": rows}]},
        budget_ms=None,
    )  # fmt: skip
    assert (answer.model_name, answer.model_version, answer.id) == ("jpcp", "3", "req-1")
    (output,) = answer.outputs
    assert output.name == "predict" and output.datatype == "FP64" and list(output.shape) == [3]
    assert np.allclose(list(output.contents.fp64_contents), rest["outputs"][0]["data"])
    assert np.allclose(list(output.contents.fp64_contents), [1.0, 2.0, 3.0], atol=1e-6)


def test_raw_input_contents_over_grpc(grpc_stub):
    stub, _ = grpc_stub
    request = pb.ModelInferRequest(
        model_name="jpcp",
        inputs=[
            pb.ModelInferRequest.InferInputTensor(name="input-0", datatype="FP32", shape=[1, 3])
        ],
        raw_input_contents=[np.array([1.0, 1.0, 1.0], dtype="<f4").tobytes()],
    )
    (output,) = stub.ModelInfer(request).outputs
    assert np.allclose(list(output.contents.fp64_contents), [6.0], atol=1e-5)


@pytest.mark.parametrize(
    ("request_", "code", "fragment"),
    [
        (pb.ModelInferRequest(model_name="nope", inputs=[_tensor("input-0", "FP64", [3],
                                                                 fp64_contents=[1, 1, 1])]),
         grpc.StatusCode.NOT_FOUND, "nope"),
        (pb.ModelInferRequest(model_name="jpcp", inputs=[_tensor("cpu", "FP64", [1],
                                                                 fp64_contents=[1.0])]),
         grpc.StatusCode.INVALID_ARGUMENT, "missing ['mem', 'nodes']"),
        (pb.ModelInferRequest(model_name="../reload"), grpc.StatusCode.INVALID_ARGUMENT,
         "not a valid name"),
        (pb.ModelInferRequest(model_name="jpcp", model_version="3/../../reload"),
         grpc.StatusCode.INVALID_ARGUMENT, "not a valid name"),
        (pb.ModelInferRequest(model_name="jpcp", inputs=[_tensor("x", "FP16", [1])]),
         grpc.StatusCode.INVALID_ARGUMENT, "raw_input_contents"),
    ],
)  # fmt: skip
def test_errors_carry_the_protocols_codes_and_the_rest_message(grpc_stub, request_, code, fragment):
    stub, _ = grpc_stub
    with pytest.raises(grpc.RpcError) as caught:
        stub.ModelInfer(request_)
    assert caught.value.code() == code
    assert fragment in caught.value.details()


def test_a_deadline_becomes_the_request_budget():
    """The model server sheds work that cannot finish in time; it can only if it is told."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["budget"] = request.headers.get("X-ExaMLOps-Budget-Ms")
        return httpx.Response(504, json={"error": "deadline exceeded before the model ran"})

    handle = oip_grpc.start_in_thread(
        "127.0.0.1", 0, "http://model-server", transport=httpx.MockTransport(handler)
    )
    try:
        stub = pb_grpc.GRPCInferenceServiceStub(grpc.insecure_channel(f"127.0.0.1:{handle.port}"))
        with pytest.raises(grpc.RpcError) as caught:
            stub.ModelInfer(pb.ModelInferRequest(model_name="jpcp"), timeout=2.0)
        assert caught.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        # The deadline travels as a rounded grpc-timeout and is measured again on the server's
        # clock, so a few milliseconds either way are expected.
        assert 1500 < int(seen["budget"]) <= 2100
        seen.clear()
        with pytest.raises(grpc.RpcError):
            stub.ModelInfer(pb.ModelInferRequest(model_name="jpcp"))  # no deadline
        assert seen["budget"] is None
    finally:
        handle.stop(grace=1)


def test_an_unreachable_model_server_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    handle = oip_grpc.start_in_thread(
        "127.0.0.1", 0, "http://model-server", transport=httpx.MockTransport(handler)
    )
    try:
        stub = pb_grpc.GRPCInferenceServiceStub(grpc.insecure_channel(f"127.0.0.1:{handle.port}"))
        with pytest.raises(grpc.RpcError) as caught:
            stub.ServerMetadata(pb.ServerMetadataRequest())
        assert caught.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        handle.stop(grace=1)


def test_the_message_size_limit_is_enforced(server):  # noqa: F811
    handle = oip_grpc.start_in_thread(
        "127.0.0.1", 0, "http://model-server", max_message_bytes=1024,
        transport=httpx.ASGITransport(app=_rest_app(server)),
    )  # fmt: skip
    try:
        stub = pb_grpc.GRPCInferenceServiceStub(grpc.insecure_channel(f"127.0.0.1:{handle.port}"))
        big = pb.ModelInferRequest(
            model_name="jpcp",
            inputs=[_tensor("input-0", "FP64", [300, 3], fp64_contents=[0.0] * 900)],
        )
        with pytest.raises(grpc.RpcError) as caught:
            stub.ModelInfer(big)
        assert caught.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        handle.stop(grace=1)


def test_the_model_server_starts_it_on_its_own_host_beside_rest(monkeypatch):
    for p in (str(REPO), str(REPO / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from serving.oip_grpc import server as grpc_module
    from serving.ray_serving import app as rs_app

    started: list = []

    class FakeHandle:
        port = 8081

        def stop(self, grace: float = 5.0) -> None:
            started.append("stopped")

    def fake_start(host, port, rest_base_url, *, max_message_bytes):
        started.append((host, port, rest_base_url, max_message_bytes))
        return FakeHandle()

    handlers: dict = {}
    monkeypatch.setattr(grpc_module, "start_in_thread", fake_start)
    monkeypatch.setattr(rs_app.ray, "init", lambda **_kw: None)
    monkeypatch.setattr(rs_app.ray, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app.serve, "start", lambda **_kw: None)
    monkeypatch.setattr(rs_app.serve, "run", lambda *a, **kw: None)
    monkeypatch.setattr(rs_app.serve, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app, "_prepare_ray_environment", lambda: None)
    monkeypatch.setattr(rs_app, "SERVE_HOST", "127.0.0.1")
    monkeypatch.setattr(rs_app, "GRPC_PORT", 8081)
    monkeypatch.setattr(rs_app.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(
        rs_app.time,
        "sleep",
        lambda _s: handlers[rs_app.signal.SIGTERM](rs_app.signal.SIGTERM, None),
    )
    rs_app.main()
    assert started == [
        ("127.0.0.1", 8081, f"http://127.0.0.1:{rs_app.SERVE_PORT}", rs_app.GRPC_MAX_MESSAGE_BYTES),
        "stopped",
    ]


def test_the_rest_body_is_valid_json():
    """What request_to_json builds is what httpx will serialise: no bytes, no numpy scalars."""
    typed = pb.ModelInferRequest(
        inputs=[_tensor("b", "BYTES", [1], bytes_contents=[b"x"]),
                _tensor("u", "UINT64", [1], uint64_contents=[2**63])],
    )  # fmt: skip
    assert json.loads(json.dumps(oip_grpc.request_to_json(typed)))["inputs"][0]["data"] == ["x"]
    raw = pb.ModelInferRequest(
        inputs=[pb.ModelInferRequest.InferInputTensor(name="x", datatype="UINT64", shape=[1])],
        raw_input_contents=[np.array([2**63], dtype="<u8").tobytes()],
    )
    assert json.loads(json.dumps(oip_grpc.request_to_json(raw)))["inputs"][0]["data"] == [2**63]


def test_a_grpc_front_end_that_cannot_start_leaves_rest_serving(monkeypatch, caplog):
    """A port in use must not take inference down: REST keeps serving, the failure is logged."""
    for p in (str(REPO), str(REPO / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from serving.oip_grpc import server as grpc_module
    from serving.ray_serving import app as rs_app

    def refuse(*_a, **_kw):
        raise OSError("could not bind the OIP gRPC server to 0.0.0.0:8081")

    handlers: dict = {}
    reached_loop: list = []
    monkeypatch.setattr(grpc_module, "start_in_thread", refuse)
    monkeypatch.setattr(rs_app.ray, "init", lambda **_kw: None)
    monkeypatch.setattr(rs_app.ray, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app.serve, "start", lambda **_kw: None)
    monkeypatch.setattr(rs_app.serve, "run", lambda *a, **kw: None)
    monkeypatch.setattr(rs_app.serve, "shutdown", lambda: None)
    monkeypatch.setattr(rs_app, "_prepare_ray_environment", lambda: None)
    monkeypatch.setattr(rs_app, "GRPC_PORT", 8081)
    monkeypatch.setattr(rs_app.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    def tick(_s):
        reached_loop.append(True)  # main() got past start-up and is serving
        handlers[rs_app.signal.SIGTERM](rs_app.signal.SIGTERM, None)

    monkeypatch.setattr(rs_app.time, "sleep", tick)
    with caplog.at_level("ERROR", logger="ray_serving"):
        rs_app.main()
    assert reached_loop == [True]
    assert "OIP gRPC server did not start on port 8081" in caplog.text


def test_the_verified_identity_reaches_rest_and_nothing_else_does():
    """What the gateway (and the mutual-TLS hop) assert travels on; other metadata does not."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"name": "examlops-ray-serving", "version": "x"})

    handle = oip_grpc.start_in_thread(
        "127.0.0.1", 0, "http://model-server", transport=httpx.MockTransport(handler)
    )
    try:
        stub = pb_grpc.GRPCInferenceServiceStub(grpc.insecure_channel(f"127.0.0.1:{handle.port}"))
        stub.ServerMetadata(
            pb.ServerMetadataRequest(),
            metadata=[
                ("x-examlops-tenant", "acme"),
                ("x-examlops-project", "research"),
                ("x-forwarded-client-cert", "URI=spiffe://examlops.internal/gateway"),
                ("authorization", "Bearer exa-not-forwarded"),
                ("x-other", "not-forwarded"),
            ],
        )
    finally:
        handle.stop(grace=1)
    assert seen["x-examlops-tenant"] == "acme" and seen["x-examlops-project"] == "research"
    assert seen["x-forwarded-client-cert"] == "URI=spiffe://examlops.internal/gateway"
    assert "authorization" not in seen and "x-other" not in seen
