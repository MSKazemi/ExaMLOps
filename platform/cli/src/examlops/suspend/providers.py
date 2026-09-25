"""Register the ``suspend_backend`` provider domain (ADR 0109 decision 5, ADR 0074).

Importing this module registers the built-ins. Third-party backends load from the
``exa.providers.suspend_backend`` entry-point group; the default is ``checkpoint-only``.
"""

from __future__ import annotations

from examlops.providers import register_provider

from .backends import CheckpointOnlyBackend, MockBackend
from .engine import VLLMSleepBackend
from .tiers import TieredTrainingCheckpointBackend
from .training import TrainingCheckpointBackend

DOMAIN = "suspend_backend"

register_provider(DOMAIN, "checkpoint-only", CheckpointOnlyBackend, default=True)
register_provider(DOMAIN, "training-checkpoint", TrainingCheckpointBackend)
register_provider(DOMAIN, "tiered-training-checkpoint", TieredTrainingCheckpointBackend)
register_provider(DOMAIN, "vllm-sleep", VLLMSleepBackend)
register_provider(DOMAIN, "mock", MockBackend)
