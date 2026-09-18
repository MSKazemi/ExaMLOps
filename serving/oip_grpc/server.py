"""The model server's gRPC front end for the Open Inference Protocol v2 (ADR 0126).

Every RPC of ``inference.GRPCInferenceService`` is answered by the model server's own REST
implementation of the same protocol, called over loopback: ``ModelInfer`` becomes
``POST /v2/models/{name}[/versions/{version}]/infer``, ``ModelMetadata`` a ``GET`` of the model's
metadata, and so on. So there is one implementation of the protocol's semantics — input
validation, deadlines, load shedding, the retry budget, error messages — and gRPC cannot drift
from REST. The cost is one JSON encoding per request on loopback.

* **Tensors.** Inputs arrive either typed (``contents``) or as ``raw_input_contents``: little-endian
  bytes, one entry per input, with ``BYTES`` elements each prefixed by a 4-byte length. Both become
  the REST form's flat ``data``. ``FP16`` is accepted raw only, as the protocol requires. Outputs
  go back typed.
* **Deadlines.** A gRPC deadline becomes the request's budget (``X-ExaMLOps-Budget-Ms``), so the
  model server sheds a request that cannot finish in time rather than running it for nobody.
* **Errors.** The REST status maps to the gRPC status code the protocol's clients expect: 400 →
  ``INVALID_ARGUMENT``, 404 → ``NOT_FOUND``, 429 → ``RESOURCE_EXHAUSTED``, 503 → ``UNAVAILABLE``,
  504 → ``DEADLINE_EXCEEDED``, other 5xx → ``INTERNAL``. The REST error message is the detail.
* **Identity passes through.** The verified identity the serving gateway sets (``x-examlops-tenant``,
  ``-principal``, ``-project``) and ``x-forwarded-client-cert`` from the mutual-TLS hop reach the
  REST implementation as the headers they would have been over REST. Nothing else is forwarded.
* **Names are path segments.** A model name or version that is not a plain segment is refused
  before anything is called, so a name like ``../reload`` can never reach another route.

``start_in_thread`` runs the server on its own event loop, beside Ray Serve in ``main()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import struct
import threading
from dataclasses import dataclass
from typing import Any

import grpc
import httpx
import numpy as np

from serving.budgets import HEADER as BUDGET_HEADER
from serving.oip_grpc import open_inference_grpc_pb2 as pb
from serving.oip_grpc import open_inference_grpc_pb2_grpc as pb_grpc

_log = logging.getLogger("examlops.serving.oip_grpc")

# The typed field of InferTensorContents that carries each datatype. FP16 and BF16 have none: the
# protocol sends them in raw_input_contents only.
_CONTENTS = {
    "BOOL": "bool_contents",
    "INT8": "int_contents",
    "INT16": "int_contents",
    "INT32": "int_contents",
    "INT64": "int64_contents",
    "UINT8": "uint_contents",
    "UINT16": "uint_contents",
    "UINT32": "uint_contents",
    "UINT64": "uint64_contents",
    "FP32": "fp32_contents",
    "FP64": "fp64_contents",
    "BYTES": "bytes_contents",
}
# numpy dtypes of the protocol's raw (little-endian) encoding.
_RAW = {
    "BOOL": "?",
    "INT8": "<i1",
    "INT16": "<i2",
    "INT32": "<i4",
    "INT64": "<i8",
    "UINT8": "<u1",
    "UINT16": "<u2",
    "UINT32": "<u4",
    "UINT64": "<u8",
    "FP16": "<f2",
    "FP32": "<f4",
    "FP64": "<f8",
}
_STATUS = {
    400: grpc.StatusCode.INVALID_ARGUMENT,
    401: grpc.StatusCode.UNAUTHENTICATED,
    403: grpc.StatusCode.PERMISSION_DENIED,
    404: grpc.StatusCode.NOT_FOUND,
    413: grpc.StatusCode.RESOURCE_EXHAUSTED,
    422: grpc.StatusCode.INVALID_ARGUMENT,
    429: grpc.StatusCode.RESOURCE_EXHAUSTED,
    503: grpc.StatusCode.UNAVAILABLE,
    504: grpc.StatusCode.DEADLINE_EXCEEDED,
}
# Request metadata passed on to the REST call: what the gateway and the mutual-TLS hop assert.
FORWARDED_METADATA = (
    "x-examlops-tenant",
    "x-examlops-principal",
    "x-examlops-project",
    "x-forwarded-client-cert",
)
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_INT64_MAX = 2**63 - 1


class BadRequest(ValueError):
    """A request the protocol does not allow; answered with INVALID_ARGUMENT."""


def status_for(http_status: int) -> grpc.StatusCode:
    """The gRPC status code for a REST answer's HTTP status."""
    if http_status in _STATUS:
        return _STATUS[http_status]
    return grpc.StatusCode.INTERNAL if http_status >= 500 else grpc.StatusCode.INVALID_ARGUMENT


# ── parameters ────────────────────────────────────────────────────────────────


def param_to_json(param: pb.InferParameter) -> Any:
    which = param.WhichOneof("parameter_choice")
    return getattr(param, which) if which else None


def param_from_json(value: Any) -> pb.InferParameter:
    if isinstance(value, bool):  # before int: bool is an int
        return pb.InferParameter(bool_param=value)
    if isinstance(value, int):
        if value > _INT64_MAX:
            return pb.InferParameter(uint64_param=value)
        return pb.InferParameter(int64_param=value)
    if isinstance(value, float):
        return pb.InferParameter(double_param=value)
    if isinstance(value, str):
        return pb.InferParameter(string_param=value)
    return pb.InferParameter(string_param=json.dumps(value, separators=(",", ":")))


def _params_to_json(params) -> dict[str, Any]:
    return {key: param_to_json(value) for key, value in params.items()}


# ── tensors ───────────────────────────────────────────────────────────────────


def _elements(shape: list[int]) -> int:
    if any(dim < 0 for dim in shape):
        raise BadRequest(f"shape {shape} has a negative dimension")
    return math.prod(shape) if shape else 1


def _utf8(value: bytes, name: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequest(f"input {name!r}: a BYTES element is not UTF-8 text") from exc


def decode_raw(datatype: str, raw: bytes, shape: list[int], name: str) -> list[Any]:
    """The flat element list of one input sent as raw little-endian bytes."""
    count = _elements(shape)
    if datatype == "BYTES":
        out: list[Any] = []
        offset = 0
        while offset < len(raw):
            if offset + 4 > len(raw):
                raise BadRequest(f"input {name!r}: truncated BYTES length prefix")
            (length,) = struct.unpack_from("<I", raw, offset)
            offset += 4
            if offset + length > len(raw):
                raise BadRequest(f"input {name!r}: BYTES element runs past the end")
            out.append(_utf8(raw[offset : offset + length], name))
            offset += length
        if len(out) != count:
            raise BadRequest(f"input {name!r}: {len(out)} BYTES elements for shape {shape}")
        return out
    dtype = _RAW.get(datatype)
    if dtype is None:
        raise BadRequest(f"input {name!r}: unsupported datatype {datatype!r}")
    expected = count * np.dtype(dtype).itemsize
    if len(raw) != expected:
        raise BadRequest(
            f"input {name!r}: {len(raw)} raw bytes, but {datatype} with shape {shape} needs "
            f"{expected}"
        )
    return np.frombuffer(raw, dtype=dtype).tolist()


def request_to_json(request: pb.ModelInferRequest) -> dict[str, Any]:
    """The REST body of a gRPC ModelInferRequest."""
    raw = list(request.raw_input_contents)
    if raw and len(raw) != len(request.inputs):
        raise BadRequest(
            f"raw_input_contents has {len(raw)} entries for {len(request.inputs)} inputs"
        )
    inputs = []
    for index, tensor in enumerate(request.inputs):
        shape = list(tensor.shape)
        if raw:
            if tensor.HasField("contents"):
                raise BadRequest(f"input {tensor.name!r}: contents and raw_input_contents both set")
            data = decode_raw(tensor.datatype, raw[index], shape, tensor.name)
        else:
            field = _CONTENTS.get(tensor.datatype)
            if field is None:
                raise BadRequest(
                    f"input {tensor.name!r}: datatype {tensor.datatype!r} must be sent in "
                    "raw_input_contents"
                )
            values = list(getattr(tensor.contents, field))
            data = [_utf8(v, tensor.name) for v in values] if tensor.datatype == "BYTES" else values
        entry: dict[str, Any] = {
            "name": tensor.name,
            "shape": shape,
            "datatype": tensor.datatype,
            "data": data,
        }
        if tensor.parameters:
            entry["parameters"] = _params_to_json(tensor.parameters)
        inputs.append(entry)
    body: dict[str, Any] = {"inputs": inputs}
    if request.id:
        body["id"] = request.id
    if request.parameters:
        body["parameters"] = _params_to_json(request.parameters)
    if request.outputs:
        body["outputs"] = [
            {
                "name": o.name,
                **({"parameters": _params_to_json(o.parameters)} if o.parameters else {}),
            }
            for o in request.outputs
        ]
    return body


def _flatten(data: Any) -> list[Any]:
    if not isinstance(data, list):
        return [data]
    flat: list[Any] = []
    for item in data:
        flat.extend(_flatten(item))
    return flat


def response_from_json(body: dict[str, Any]) -> pb.ModelInferResponse:
    """The gRPC ModelInferResponse of a REST infer answer."""
    response = pb.ModelInferResponse(
        model_name=str(body.get("model_name", "")),
        model_version=str(body.get("model_version") or ""),
        id=str(body.get("id") or ""),
    )
    for key, value in (body.get("parameters") or {}).items():
        response.parameters[key].CopyFrom(param_from_json(value))
    for out in body.get("outputs") or []:
        datatype = str(out.get("datatype", ""))
        field = _CONTENTS.get(datatype)
        if field is None:
            raise ValueError(
                f"output {out.get('name')!r} has datatype {datatype!r}, not sendable typed"
            )
        data = _flatten(out.get("data", []))
        shape = [int(d) for d in out.get("shape") or [len(data)]]
        tensor = response.outputs.add(name=str(out.get("name", "")), datatype=datatype, shape=shape)
        for key, value in (out.get("parameters") or {}).items():
            tensor.parameters[key].CopyFrom(param_from_json(value))
        if datatype == "BYTES":
            data = [v.encode("utf-8") if isinstance(v, str) else bytes(v) for v in data]
        getattr(tensor.contents, field).extend(data)
    return response


# ── the service ───────────────────────────────────────────────────────────────


def _segment(value: str, what: str) -> str:
    if not _SEGMENT.match(value) or ".." in value:
        raise BadRequest(f"{what} {value!r} is not a valid name")
    return value


def _model_path(name: str, version: str) -> str:
    path = f"/v2/models/{_segment(name, 'model name')}"
    if version:
        path += f"/versions/{_segment(version, 'model version')}"
    return path


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or body)
    return str(body)


class OipServicer(pb_grpc.GRPCInferenceServiceServicer):
    """``inference.GRPCInferenceService`` on top of the model server's REST implementation."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )
        self._timeout = timeout

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        context: grpc.aio.ServicerContext,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        remaining = context.time_remaining()
        headers = {
            key: value
            for key, value in (context.invocation_metadata() or ())
            if key in FORWARDED_METADATA and isinstance(value, str)
        }
        timeout = self._timeout
        if remaining is not None:
            if remaining <= 0:
                await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
            headers[BUDGET_HEADER] = str(max(1, int(remaining * 1000)))
            timeout = min(timeout, remaining)
        try:
            return await self._http.request(
                method, path, json=body, headers=headers, timeout=timeout
            )
        except httpx.TimeoutException:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED, "the model server did not answer in time"
            )
        except httpx.TransportError as exc:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE, f"the model server is unreachable: {exc}"
            )
        raise AssertionError("unreachable")  # context.abort raises

    async def _checked(self, context, method: str, path: str, body=None) -> Any:
        response = await self._call(context, method, path, body)
        if response.status_code != 200:
            await context.abort(status_for(response.status_code), _error_detail(response))
        return response.json()

    async def _invalid(self, context, exc: BadRequest):
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

    async def ServerLive(self, request, context):  # noqa: N802 - the protocol's name
        response = await self._call(context, "GET", "/v2/health/live")
        return pb.ServerLiveResponse(live=response.status_code == 200)

    async def ServerReady(self, request, context):  # noqa: N802
        response = await self._call(context, "GET", "/v2/health/ready")
        return pb.ServerReadyResponse(ready=response.status_code == 200)

    async def ModelReady(self, request, context):  # noqa: N802
        try:
            path = _model_path(request.name, request.version) + "/ready"
        except BadRequest as exc:
            await self._invalid(context, exc)
        response = await self._call(context, "GET", path)
        return pb.ModelReadyResponse(ready=response.status_code == 200)

    async def ServerMetadata(self, request, context):  # noqa: N802
        body = await self._checked(context, "GET", "/v2")
        return pb.ServerMetadataResponse(
            name=str(body.get("name", "")),
            version=str(body.get("version", "")),
            extensions=[str(e) for e in body.get("extensions", [])],
        )

    async def ModelMetadata(self, request, context):  # noqa: N802
        try:
            path = _model_path(request.name, request.version)
        except BadRequest as exc:
            await self._invalid(context, exc)
        body = await self._checked(context, "GET", path)

        def tensors(items):
            return [
                pb.ModelMetadataResponse.TensorMetadata(
                    name=str(t.get("name", "")),
                    datatype=str(t.get("datatype", "")),
                    shape=[int(d) for d in t.get("shape", [])],
                )
                for t in items or []
            ]

        return pb.ModelMetadataResponse(
            name=str(body.get("name", "")),
            versions=[str(v) for v in body.get("versions", [])],
            platform=str(body.get("platform", "")),
            inputs=tensors(body.get("inputs")),
            outputs=tensors(body.get("outputs")),
        )

    async def ModelInfer(self, request, context):  # noqa: N802
        try:
            path = _model_path(request.model_name, request.model_version) + "/infer"
            body = request_to_json(request)
        except BadRequest as exc:
            await self._invalid(context, exc)
        answer = await self._checked(context, "POST", path, body)
        try:
            return response_from_json(answer)
        except (ValueError, TypeError) as exc:
            await context.abort(
                grpc.StatusCode.INTERNAL, f"the model's answer cannot be sent: {exc}"
            )


# ── running it ────────────────────────────────────────────────────────────────


def build_server(
    servicer: OipServicer, address: str, *, max_message_bytes: int
) -> tuple[grpc.aio.Server, int]:
    """A gRPC server for ``servicer`` bound to ``address``, and the port it bound."""
    options = [
        ("grpc.max_receive_message_length", max_message_bytes),
        ("grpc.max_send_message_length", max_message_bytes),
    ]
    server = grpc.aio.server(options=options)
    pb_grpc.add_GRPCInferenceServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port(address)
    if port == 0:
        raise OSError(f"could not bind the OIP gRPC server to {address}")
    return server, port


@dataclass
class GrpcHandle:
    """A gRPC server running on its own thread and event loop."""

    port: int
    _loop: asyncio.AbstractEventLoop
    _thread: threading.Thread
    _server: grpc.aio.Server
    _servicer: OipServicer

    def stop(self, grace: float = 5.0) -> None:
        async def _stop() -> None:
            await self._server.stop(grace)
            await self._servicer.aclose()

        asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(grace + 5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(grace + 5)


def start_in_thread(
    host: str,
    port: int,
    rest_base_url: str,
    *,
    max_message_bytes: int = 8 * 1024 * 1024,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GrpcHandle:
    """Start the OIP gRPC server on ``host:port`` (``port`` 0 picks one) and return its handle."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict[str, Any] = {}

    def run() -> None:
        asyncio.set_event_loop(loop)

        async def boot() -> None:
            servicer = OipServicer(rest_base_url, transport=transport)
            server, bound = build_server(
                servicer, f"{host}:{port}", max_message_bytes=max_message_bytes
            )
            await server.start()
            state.update(server=server, servicer=servicer, port=bound)

        try:
            loop.run_until_complete(boot())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            state["error"] = exc
            ready.set()
            return
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="oip-grpc", daemon=True)
    thread.start()
    ready.wait(30)
    if "error" in state:
        raise state["error"]
    if "server" not in state:
        raise TimeoutError("the OIP gRPC server did not start within 30 s")
    _log.info("OIP gRPC server on %s:%d → %s", host, state["port"], rest_base_url)
    return GrpcHandle(state["port"], loop, thread, state["server"], state["servicer"])
