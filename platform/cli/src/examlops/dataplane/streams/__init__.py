"""Dataplane streaming surface (ADR 0130/0131, Plan 2).

The facade re-exports the import-light pieces: the stream types, the model schema registry and the
telemetry seam (stdlib only). Everything that pulls in an HTTP client, a database or a broker is
imported from its own module, so importing ``examlops.dataplane.streams`` stays cheap on an HPC
node:

- ``streams.ingress`` — ``StreamIngress`` (validate → permit → rate → infer → reply → telemetry);
- ``streams.client`` — ``RayPipelineClient`` and the inference status mapping;
- ``streams.drift`` — ``DriftAggregator`` and the ``RetrainTrigger`` seam;
- ``streams.metrics`` — the ``dataplane_stream_*`` Prometheus series;
- ``streams.bindings`` — the ``dataplane_streams`` catalog, pack sync and project tenancy;
- ``streams.connectors`` / ``streams.kafka_stream`` / ``streams.dlq`` — inbound connectors.
"""

from examlops.dataplane.streams.schema import (
    ModelSchemaRegistry,
    build_body,
    reset_default_registry,
    validate,
)
from examlops.dataplane.streams.telemetry import (
    DbTelemetrySink,
    EmbeddingStats,
    TelemetryRecord,
    TelemetrySink,
    TelemetrySpool,
    TelemetryWriteError,
)
from examlops.dataplane.streams.types import (
    InferenceResult,
    Outcome,
    StreamBinding,
    StreamLimits,
    StreamRequest,
)

__all__ = [
    "DbTelemetrySink",
    "EmbeddingStats",
    "InferenceResult",
    "ModelSchemaRegistry",
    "Outcome",
    "StreamBinding",
    "StreamLimits",
    "StreamRequest",
    "TelemetryRecord",
    "TelemetrySink",
    "TelemetrySpool",
    "TelemetryWriteError",
    "build_body",
    "reset_default_registry",
    "validate",
]
