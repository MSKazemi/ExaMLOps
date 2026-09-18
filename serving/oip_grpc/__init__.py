"""Open Inference Protocol v2 over gRPC for the model server (ADR 0126).

``open_inference_grpc.proto`` is the protocol's own definition, copied verbatim from the Open
Inference Protocol specification (kserve/open-inference-protocol, Apache 2.0), so KServe, Triton
and MLServer gRPC clients work unchanged. The ``*_pb2*`` modules are generated from it and never
edited by hand; regenerate them after changing the proto (the command is in ``PROTO_SHA256``'s
comment). ``server`` is the gRPC front end itself.
"""

from __future__ import annotations

# sha256 of open_inference_grpc.proto the committed stubs were generated from. After changing the
# proto, regenerate from the repository root with grpcio-tools 1.80.0 (the stubs then require
# grpcio >= 1.80 and protobuf >= 6.31.1 at run time) and update this value:
#   python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. --pyi_out=. \
#       serving/oip_grpc/open_inference_grpc.proto
PROTO_SHA256 = "0f715460d60b014a23e06ac8e768cfa3d8336221cdb66bd7aeeab6dc27620b0f"
