"""Unit tests for the Phase 5 framework-adapter layer."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if p not in sys.path:
        sys.path.insert(0, p)

# ── upstream-library guard ───────────────────────────────────────────────────
# `seanergys_modelzoo` is an UPSTREAM library, not part of ExaMLOps (ADR 0094):
# the platform core never imports it — only the use-case pack does, through the
# loader seam. It is therefore not vendored in the public tree; CI and the
# deploy node fetch it from its own repo. Skip rather than fail when absent.
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (REPO_ROOT / "modelzoo"))
if not (_MZ / "seanergys_modelzoo").is_dir():
    pytest.skip(
        "seanergys_modelzoo not present — upstream library fetched at deploy/CI "
        "time. Set EXAMLOPS_MODELZOO_DIR to a checkout to run these tests.",
        allow_module_level=True,
    )
if str(_MZ) not in sys.path:
    sys.path.insert(0, str(_MZ))


from seanergys_modelzoo.models.common import framework_adapter as fa  # noqa: E402

# ── get_adapter / adapter_for ────────────────────────────────────────────────


class TestRegistry:
    def test_unknown_flavour_raises(self):
        with pytest.raises(ValueError, match="Unknown framework"):
            fa.get_adapter("vllm")

    def test_default_is_sklearn(self):
        assert fa.get_adapter(None).flavour == "sklearn"
        assert fa.get_adapter("").flavour == "sklearn"

    def test_each_flavour_returns_matching_adapter(self):
        assert isinstance(fa.get_adapter("sklearn"), fa.SklearnFrameworkAdapter)
        assert isinstance(fa.get_adapter("pytorch"), fa.PyTorchFrameworkAdapter)
        assert isinstance(fa.get_adapter("huggingface"), fa.HuggingFaceFrameworkAdapter)

    def test_adapter_for_falls_back_to_sklearn_for_magicmock(self):
        """A MagicMock will return a MagicMock for any attribute — must not break."""
        model = MagicMock()
        adapter = fa.adapter_for(model)
        assert adapter.flavour == "sklearn"

    def test_adapter_for_picks_huggingface_when_declared(self):
        model = SimpleNamespace(framework="huggingface")
        assert fa.adapter_for(model).flavour == "huggingface"

    def test_register_adapter_extends_the_registry(self):
        class FakeAdapter:
            flavour = "fake"

            def fit(self, m, loader):
                return {}

            def predict(self, m, x):
                return None

            def save(self, m, p):
                return p

            def load(self, m, p):
                return None

            def log_mlflow(self, m, n):
                pass

        try:
            fa.register_adapter("fake", FakeAdapter)
            assert isinstance(fa.get_adapter("fake"), FakeAdapter)
        finally:
            fa._ADAPTERS.pop("fake", None)


# ── SklearnFrameworkAdapter — round-trip on a real estimator ─────────────────


class TestSklearnAdapter:
    def test_save_and_load_roundtrip(self, tmp_path):
        from sklearn.ensemble import RandomForestRegressor

        est = RandomForestRegressor(n_estimators=2, random_state=0)
        est.fit([[1.0], [2.0]], [10.0, 20.0])

        model = SimpleNamespace(estimator=est)
        adapter = fa.SklearnFrameworkAdapter()
        path = tmp_path / "rf.joblib"

        adapter.save(model, path)
        assert path.exists()

        # Wipe estimator, reload through the adapter, predictions match.
        loaded_target = SimpleNamespace(estimator=None)
        adapter.load(loaded_target, path)
        original = est.predict([[1.5]])
        roundtripped = loaded_target.estimator.predict([[1.5]])
        assert (original == roundtripped).all()

    def test_log_mlflow_calls_sklearn_flavour(self):
        adapter = fa.SklearnFrameworkAdapter()
        model = SimpleNamespace(estimator=MagicMock())
        with patch("mlflow.sklearn.log_model") as log:
            adapter.log_mlflow(model, registered_name="reg")
        log.assert_called_once()
        # registered_model_name kwarg matches what the pipeline passes.
        assert log.call_args.kwargs["registered_model_name"] == "reg"


# ── PyTorch + HuggingFace adapters call the right flavour ────────────────────


class TestPyTorchAdapter:
    def test_log_mlflow_calls_pytorch_flavour(self):
        adapter = fa.PyTorchFrameworkAdapter()
        model = SimpleNamespace(estimator=MagicMock())
        # Patch the attribute on the imported module itself so the adapter's
        # local import resolves to our mock.
        with patch("mlflow.pytorch.log_model", create=True) as log:
            adapter.log_mlflow(model, registered_name="torch-reg")
        log.assert_called_once()
        assert log.call_args.kwargs["registered_model_name"] == "torch-reg"


class TestHuggingFaceAdapter:
    def test_log_mlflow_passes_tokenizer_when_present(self):
        adapter = fa.HuggingFaceFrameworkAdapter()
        model = SimpleNamespace(estimator=MagicMock(), tokenizer=MagicMock())
        with patch("mlflow.transformers.log_model", create=True) as log:
            adapter.log_mlflow(model, registered_name="hf-reg")
        log.assert_called_once()
        bundle = log.call_args.kwargs["transformers_model"]
        assert "model" in bundle
        assert "tokenizer" in bundle

    def test_log_mlflow_omits_tokenizer_when_absent(self):
        adapter = fa.HuggingFaceFrameworkAdapter()
        model = SimpleNamespace(estimator=MagicMock(), tokenizer=None)
        with patch("mlflow.transformers.log_model", create=True) as log:
            adapter.log_mlflow(model, registered_name="hf-reg")
        bundle = log.call_args.kwargs["transformers_model"]
        assert "tokenizer" not in bundle


# ── LLM/agent skeleton ───────────────────────────────────────────────────────


class TestLLMAgentSkeleton:
    def test_chat_must_be_overridden(self):
        from seanergys_modelzoo.models.common.huggingface_seanergys_model import (
            SeanergysLLMAgent,
        )

        # SeanergysModel is abstract — provide minimal stubs for the abstract
        # methods so we can instantiate and prove ``chat`` is the only piece
        # left for concrete LLM agents to implement.
        from seanergys_modelzoo.models.common.seanergys_model import SeanergysModelTask
        from seanergys_modelzoo.models.common.seanergys_model_metadata import (
            SeanergysModelMetadata,
        )

        class StubAgent(SeanergysLLMAgent):
            def build_model(self):
                pass

            def train(self, *a, **k):
                return {}

            def predict(self, *a, **k):
                return None

            def evaluate(self, *a, **k):
                return []

            def save(self, *a, **k):
                return True

            @classmethod
            def load(cls, *a, **k):
                return None  # not exercised here

        agent = StubAgent(
            metadata=SeanergysModelMetadata(name="stub-agent"),
            task_type=SeanergysModelTask.CLASSIFICATION,
        )
        assert agent.tool_specs == []
        with pytest.raises(NotImplementedError):
            agent.chat([{"role": "user", "content": "hi"}])

    def test_framework_is_huggingface(self):
        from seanergys_modelzoo.models.common.huggingface_seanergys_model import (
            SeanergysHuggingFaceModel,
            SeanergysLLMAgent,
        )

        assert SeanergysHuggingFaceModel.framework == "huggingface"
        assert SeanergysLLMAgent.framework == "huggingface"
