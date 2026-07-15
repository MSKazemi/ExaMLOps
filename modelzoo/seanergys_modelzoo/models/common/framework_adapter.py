"""
Framework adapter — Phase 5 plug-in layer for non-sklearn ML frameworks.

The platform's training/serving pipeline talks to a model via four operations:

    fit(loader)       train the underlying estimator on a SeanergysDataloader
    predict(features) run inference on a feature row / batch
    save(path)        persist the estimator to disk
    load(path)        restore an estimator from disk

For sklearn this is the existing :class:`SeanergysSklearnModel` behaviour. For
PyTorch / HuggingFace / LLM models the same four operations are needed, but
the artefact format and MLflow flavour differ.

This module introduces a thin :class:`SeanergysFrameworkAdapter` protocol so
the pipeline (``log_mlflow_task``, ``result_fetch_task``) and Ray Serve
loader can dispatch on framework without each new framework having to touch
the existing flow. The pre-existing sklearn path is wrapped without any
behavioural change.

Three flavours ship in Phase 5:

* ``"sklearn"``     — joblib + ``mlflow.sklearn``      (default; no behaviour change)
* ``"pytorch"``     — ``torch.save`` + ``mlflow.pytorch``
* ``"huggingface"`` — ``model.save_pretrained`` + ``mlflow.transformers``

Concrete framework bases (``SeanergysHuggingFaceModel`` / ``SeanergysPyTorchModel``)
declare the flavour via the ``framework`` class attribute and the pipeline
picks the right adapter automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SeanergysFrameworkAdapter(Protocol):
    """The four-method contract every framework adapter must implement."""

    flavour: str  # "sklearn" / "pytorch" / "huggingface"

    def fit(self, model: Any, loader: Any) -> dict:
        """Train *model.estimator* using a SeanergysDataloader. Returns a history dict."""
        ...

    def predict(self, model: Any, features: Any) -> Any:
        """Run inference on a numpy/tensor input and return raw predictions."""
        ...

    def save(self, model: Any, path: Path) -> Path:
        """Persist *model.estimator* to *path*. Returns the path actually written."""
        ...

    def load(self, model: Any, path: Path) -> Any:
        """Restore an estimator from *path* and inject it back into *model*."""
        ...

    def log_mlflow(self, model: Any, registered_name: str) -> None:
        """Log the trained estimator using the framework-appropriate MLflow flavor."""
        ...


# ─── sklearn (default — wraps the existing SeanergysSklearnModel) ────────────


class SklearnFrameworkAdapter:
    """Adapter that mirrors the existing SeanergysSklearnModel save/load path.

    Kept identical in behaviour to the legacy code so that turning the adapter
    layer on is a no-op for sklearn-backed models.
    """

    flavour = "sklearn"

    def fit(self, model: Any, loader: Any) -> dict:
        return model.train_step(loader) if hasattr(model, "train_step") else model.train(loader)

    def predict(self, model: Any, features: Any) -> Any:
        return model.estimator.predict(features)

    def save(self, model: Any, path: Path) -> Path:
        import joblib  # noqa: PLC0415

        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model.estimator, path)
        return path

    def load(self, model: Any, path: Path) -> Any:
        import joblib  # noqa: PLC0415

        estimator = joblib.load(path)
        model.estimator = estimator
        return estimator

    def log_mlflow(self, model: Any, registered_name: str) -> None:
        import mlflow.sklearn  # noqa: PLC0415

        mlflow.sklearn.log_model(
            sk_model=model.estimator,
            name="model",
            registered_model_name=registered_name,
        )


# ─── PyTorch ─────────────────────────────────────────────────────────────────


class PyTorchFrameworkAdapter:
    """Adapter for ``torch.nn.Module`` estimators.

    Models declaring ``framework = "pytorch"`` must expose:

    * ``estimator`` — a ``torch.nn.Module`` with a forward pass.
    * ``train_step(loader)`` — owns the optimiser/loss loop (frameworks vary
      too much to pin one here).
    """

    flavour = "pytorch"

    def fit(self, model: Any, loader: Any) -> dict:
        return model.train_step(loader)

    def predict(self, model: Any, features: Any) -> Any:
        import torch  # noqa: PLC0415

        model.estimator.eval()
        with torch.no_grad():
            inputs = features if isinstance(features, torch.Tensor) else torch.as_tensor(features)
            outputs = model.estimator(inputs)
            return outputs.cpu().numpy() if hasattr(outputs, "cpu") else outputs

    def save(self, model: Any, path: Path) -> Path:
        import torch  # noqa: PLC0415

        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(model.estimator.state_dict(), path)
        return path

    def load(self, model: Any, path: Path) -> Any:
        import torch  # noqa: PLC0415

        state = torch.load(path, map_location="cpu")
        model.estimator.load_state_dict(state)
        model.estimator.eval()
        return model.estimator

    def log_mlflow(self, model: Any, registered_name: str) -> None:
        import mlflow.pytorch  # noqa: PLC0415

        mlflow.pytorch.log_model(
            pytorch_model=model.estimator,
            name="model",
            registered_model_name=registered_name,
        )


# ─── HuggingFace transformers ────────────────────────────────────────────────


class HuggingFaceFrameworkAdapter:
    """Adapter for HuggingFace ``transformers`` models.

    Models declaring ``framework = "huggingface"`` should expose ``estimator``
    as a transformers pipeline / model and (optionally) ``tokenizer``.
    """

    flavour = "huggingface"

    def fit(self, model: Any, loader: Any) -> dict:
        return model.train_step(loader)

    def predict(self, model: Any, features: Any) -> Any:
        # ``features`` is expected to be a string or list[str] for text models.
        return model.estimator(features)

    def save(self, model: Any, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        model.estimator.save_pretrained(path)
        if getattr(model, "tokenizer", None) is not None:
            model.tokenizer.save_pretrained(path)
        return path

    def load(self, model: Any, path: Path) -> Any:
        from transformers import AutoModel, AutoTokenizer  # noqa: PLC0415

        model.estimator = AutoModel.from_pretrained(path)
        try:
            model.tokenizer = AutoTokenizer.from_pretrained(path)
        except Exception:  # noqa: BLE001
            model.tokenizer = None
        return model.estimator

    def log_mlflow(self, model: Any, registered_name: str) -> None:
        import mlflow.transformers  # noqa: PLC0415

        bundle: dict[str, Any] = {"model": model.estimator}
        if getattr(model, "tokenizer", None) is not None:
            bundle["tokenizer"] = model.tokenizer
        mlflow.transformers.log_model(
            transformers_model=bundle,
            name="model",
            registered_model_name=registered_name,
        )


# ─── Registry / factory ──────────────────────────────────────────────────────


_ADAPTERS: dict[str, type[SeanergysFrameworkAdapter]] = {
    "sklearn": SklearnFrameworkAdapter,
    "pytorch": PyTorchFrameworkAdapter,
    "huggingface": HuggingFaceFrameworkAdapter,
}


def get_adapter(flavour: str) -> SeanergysFrameworkAdapter:
    """Return the framework adapter instance for *flavour*."""
    flavour = (flavour or "sklearn").lower()
    cls = _ADAPTERS.get(flavour)
    if cls is None:
        raise ValueError(
            f"Unknown framework flavour: {flavour!r}. Valid: {sorted(_ADAPTERS)}"
        )
    return cls()


def adapter_for(model: Any) -> SeanergysFrameworkAdapter:
    """Pick the adapter for a model based on its ``framework`` attribute.

    Models that don't declare ``framework`` default to ``"sklearn"`` so the
    legacy SeanergysSklearnModel path Just Works without any change. A
    non-string ``framework`` (e.g. a MagicMock during tests, or a misconfigured
    subclass) is also treated as the default rather than blowing up.
    """
    flavour = getattr(model, "framework", "sklearn")
    if not isinstance(flavour, str) or not flavour:
        flavour = "sklearn"
    return get_adapter(flavour)


def register_adapter(flavour: str, adapter_cls: type[SeanergysFrameworkAdapter]) -> None:
    """Register a custom adapter class. Useful for vLLM / Triton / ONNX bases."""
    _ADAPTERS[flavour.lower()] = adapter_cls


__all__ = [
    "SeanergysFrameworkAdapter",
    "SklearnFrameworkAdapter",
    "PyTorchFrameworkAdapter",
    "HuggingFaceFrameworkAdapter",
    "get_adapter",
    "adapter_for",
    "register_adapter",
]
