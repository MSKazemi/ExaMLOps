from __future__ import annotations

from skipper.tools import (
    approvals,
    docs,
    inference,
    metrics,
    modelzoo,
    pipelines,
    platform_ops,
    registry,
    services,
    training,
)

TOOLS = [
    *registry.TOOLS,
    *inference.TOOLS,
    *metrics.TOOLS,
    *training.TOOLS,
    *approvals.TOOLS,
    *modelzoo.TOOLS,
    *services.TOOLS,
    *pipelines.TOOLS,
    *docs.TOOLS,
    *platform_ops.TOOLS,
]
