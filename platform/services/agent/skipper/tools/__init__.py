from __future__ import annotations

from skipper.tools import (
    approvals,
    baselines,
    docs,
    finops,
    inference,
    knowledge,
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
    *knowledge.TOOLS,
    *baselines.TOOLS,
    *platform_ops.TOOLS,
    *finops.TOOLS,
]
