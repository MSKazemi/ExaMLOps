"""HuggingFace base for SeanergysModel — Phase 5.

Concrete HF models subclass ``SeanergysHuggingFaceModel``. They expose
``estimator`` (a transformers model or pipeline) and optionally ``tokenizer``;
the pipeline uses ``mlflow.transformers`` for logging because of
``framework = "huggingface"``.

The class is intentionally thin — fine-tuning loops differ per task (text
classification, token classification, generation, …) so subclasses own
``train_step`` and ``predict_step``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel


class SeanergysHuggingFaceModel(SeanergysModel):
    """Base class for HuggingFace-backed Seanergys models."""

    framework: ClassVar[str] = "huggingface"

    estimator: Any = Field(
        default=None,
        description="A transformers model or pipeline instance.",
    )
    tokenizer: Any = Field(
        default=None,
        description="Optional matching tokenizer; saved alongside the model.",
    )
    pretrained_name: str | None = Field(
        default=None,
        description="HuggingFace model id used to seed the estimator (e.g. 'distilbert-base-uncased').",
    )

    def train_step(self, loader: Any) -> dict:  # pragma: no cover - subclass-specific
        raise NotImplementedError(
            f"{type(self).__name__} must implement train_step(loader)."
        )


class SeanergysLLMAgent(SeanergysHuggingFaceModel):
    """Skeleton for LLM / agentic Seanergys models — implementation deferred.

    Concrete subclasses are expected to override ``chat`` for the multi-turn
    inference path and ``train_step`` only if the agent supports fine-tuning.
    The ``system_prompt`` and ``tool_specs`` fields give the platform a stable
    hook for prompt + tool injection without each agent reinventing the wheel.

    Phase 5 ships this as a typed placeholder so Phase 6 can add a concrete
    LLM/agent without changing the registry plumbing again.
    """

    system_prompt: str | None = Field(
        default=None,
        description="System / role prompt prepended to every chat turn.",
    )
    tool_specs: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "OpenAI-style tool / function specs the agent is allowed to call. "
            "Empty list disables tool use."
        ),
    )

    def chat(self, history: list[dict[str, str]]) -> dict[str, Any]:  # pragma: no cover
        """Run a multi-turn inference step. Subclasses MUST override."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement chat(history)."
        )
