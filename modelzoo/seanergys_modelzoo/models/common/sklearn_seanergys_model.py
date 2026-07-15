from __future__ import annotations

import os
from typing import Any, Dict, Optional, Union, List, Callable, Iterable
from pathlib import Path
import time
import json
import joblib
import numpy as np
from datetime import datetime
from sklearn.base import BaseEstimator
from pydantic import Field

from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel, SeanergysModelTask
from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger
from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader


class SeanergysSklearnModel(SeanergysModel):
    """
    Scikit-learn model wrapper for Seanergys models.

    Provides a unified interface for scikit-learn estimators with support for
    training, prediction, evaluation, and model persistence.

    Supports both classification and regression tasks with automatic
    metric computation based on task type.
    """

    model_class: type = Field(..., description="The scikit-learn estimator class to instantiate")
    model_hyperparameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Hyperparameters forwarded to the estimator constructor"
    )
    training_history: Dict[str, Any] = Field(
        default_factory=dict,
        description="Training and validation metrics history"
    )
    

    def model_post_init(self, __context: Any) -> None:
        """
        Post-initialisation hook (Pydantic v2).
        Delegates to the base class then builds the estimator.
        """
        super().model_post_init(__context)
        self.build_model()


    def build_model(self) -> None:
        """
        Instantiate the scikit-learn estimator from model_class and
        model_hyperparameters.
        """
        try:
            self.estimator = self.model_class(**self.model_hyperparameters)
            self.logger.info(
                f"Built {self.model_name} with estimator: "
                f"{self.estimator.__class__.__name__}"
            )
        except Exception as e:
            self.logger.error(f"Error building estimator: {e}")
            raise

    def train(
        self,
        train_data_loader: SeanergysDataloader,
        val_data_loader: Optional[SeanergysDataloader] = None,
        training_loss: Optional[Callable] = None,
        validation_metrics: Optional[List[Callable]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train the scikit-learn estimator.

        Uses partial_fit (mini-batch) when available, otherwise collects all
        batches and calls fit in one shot.

        Args:
            train_data_loader: Dataloader providing (X, y) batches.
            val_data_loader:   Optional dataloader for validation.
            training_loss:     Callable(y_true, y_pred) -> float, optional.
            validation_metrics: List of Callable(y_true, y_pred) -> float.
            **kwargs:          Forwarded to fit / partial_fit.

        Returns:
            Nested dict with training and validation results.
        """
        self.logger.info(f"Starting training {self.model_name}")
        start_time = time.time()

        try:
            train_loss_value = None
            val_metrics_values = None
            val_time = None

            supports_partial_fit = (
                hasattr(self.estimator, "partial_fit")
                and callable(self.estimator.partial_fit)
            )

            if supports_partial_fit:
                self.logger.info(
                    f"Training on {len(train_data_loader)} batches via partial_fit "
                    f"({len(train_data_loader.dataset)} total samples)"
                )
                all_y_true, all_y_pred = [], []
                for X_batch, y_batch in train_data_loader:
                    X_np = self._to_numpy(X_batch)
                    y_np = self._to_numpy(y_batch)
                    self.estimator.partial_fit(X_np, y_np, **kwargs)
                    if training_loss:
                        all_y_pred.extend(self.estimator.predict(X_np))
                        all_y_true.extend(y_np)

                if training_loss and all_y_true:
                    train_loss_value = training_loss(
                        np.array(all_y_true), np.array(all_y_pred)
                    )

            else:
                X_train, y_train = self._extract_data_from_loader(train_data_loader)
                self.logger.info(f"Fitting on {len(X_train)} samples...")
                self.estimator.fit(X_train, y_train, **kwargs)

                if training_loss:
                    train_preds = self.estimator.predict(X_train)
                    train_loss_value = training_loss(y_train, train_preds)

            # Update state
            self.is_trained = True
            training_time = time.time() - start_time
            self.metadata.set_field("last_trained", datetime.now().isoformat())
            self.metadata.set_field("training_time", training_time)
            if train_loss_value is not None:
                self.metadata.set_field("training_loss", train_loss_value)

            self.logger.info(f"Training completed in {training_time:.2f}s")

            # Validation
            if val_data_loader is not None:
                if not validation_metrics:
                    self.logger.error(
                        "val_data_loader provided but validation_metrics is empty; "
                        "skipping validation."
                    )
                else:
                    t0_val = time.time()
                    X_val, y_val = self._extract_data_from_loader(val_data_loader)
                    val_preds = self.estimator.predict(X_val)
                    val_metrics_values = {
                        metric.__name__: metric(y_val, val_preds)
                        for metric in validation_metrics
                    }
                    val_time = time.time() - t0_val
                    self.metadata.set_field("validation_metrics", val_metrics_values)
                    self.metadata.set_field("validation_time", val_time)
                    self.logger.info(f"Validation metrics: {val_metrics_values}")

            # Persist in training_history
            self.training_history = {
                "training": {
                    "training_loss": train_loss_value,
                    "training_time": training_time,
                },
                "validation": {
                    "validation_metrics": val_metrics_values,
                    "validation_time": val_time,
                },
            }

            return self.training_history

        except Exception as e:
            self.logger.error(f"Error during training: {e}")
            raise

    def predict(
        self,
        data_loader: Union[SeanergysDataloader, Iterable],
        return_proba: bool = False,
        **kwargs
    ) -> np.ndarray:
        """
        Make predictions on input data.

        Args:
            data_loader:  A SeanergysDataloader or any array-like iterable.
            return_proba: Return class probabilities for classification tasks.
            **kwargs:     Forwarded to the estimator predict call.

        Returns:
            Predictions as a numpy array.
        """
        if not self.is_trained:
            self.logger.warning("Model has not been trained yet.")

        try:
            if isinstance(data_loader, SeanergysDataloader):
                X, _ = self._extract_data_from_loader(data_loader)
            else:
                X = np.array(data_loader)

            if return_proba and self.task_type == SeanergysModelTask.CLASSIFICATION:
                if hasattr(self.estimator, "predict_proba"):
                    predictions = self.estimator.predict_proba(X, **kwargs)
                    self.logger.info(
                        f"Generated probability predictions for {len(X)} samples"
                    )
                else:
                    self.logger.warning(
                        f"{self.estimator.__class__.__name__} does not support "
                        "predict_proba; falling back to predict."
                    )
                    predictions = self.estimator.predict(X, **kwargs)
            else:
                predictions = self.estimator.predict(X, **kwargs)
                self.logger.info(f"Generated predictions for {len(X)} samples")

            return predictions

        except Exception as e:
            self.logger.error(f"Error during prediction: {e}")
            raise
    
    def evaluate(
        self,
        data_loader: SeanergysDataloader,
        metrics: List[Callable] = None,
        **kwargs
    ) -> List:
        """
        Evaluate model performance on input data.
        
        Args:
            data_loader: A SeanergysDataloader containing input data for evaluations
            metrics: List of the metrics to use to evaluate the model, each function should take as input y_true and y_pred. If None the subclasses should implement at least one by default.
            **kwargs: Additional prediction arguments
        Returns:
            List[Score] (format depends on framework and task and metrics given in input)
        """
        y_preds = self.predict(data_loader)
        _, y_test = self._extract_data_from_loader(data_loader)
        metrics_results = []
        if metrics:
            for metric in metrics:
                res = metric(y_pred=y_preds, y_true=y_test)
                metrics_results.append(res)
        return metrics_results

    def save(
        self,
        path: Union[str, Path] = "./saved_model",
        compress: int = 3,
        **kwargs
    ) -> bool:
        """
        Persist the estimator, metadata, and supplementary info to disk.

        Args:
            path:     Destination path for the joblib model file.
            compress: joblib compression level (0–9).

        Returns:
            True on success, False on failure.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 1. Persist the fitted estimator
            joblib.dump(self.estimator, f"{os.path.join(str(path), self.model_name, '.joblib')}", compress=compress)
            self.logger.info(f"Estimator saved to {path}")

            # 2. Persist metadata (base-class helper)
            self._save_metadata(path)

            # 3. Persist supplementary info needed to reconstruct the instance
            info = {
                "model_class_module": self.model_class.__module__,
                "model_class_name": self.model_class.__name__,
                "task_type": self.task_type.name,          # store enum name
                "model_hyperparameters": self.model_hyperparameters,
                "sklearn_params": self.estimator.get_params(),
            }
            info_path = path.parent / f"{path.stem}_info.json"
            with open(info_path, "w") as f:
                json.dump(info, f, indent=2)
            self.logger.info(f"Model info saved to {info_path}")

            return True

        except Exception as e:
            self.logger.error(f"Error saving model: {e}")
            return False

    @classmethod
    def load(
        cls,
        path: Union[str, Path] = "./saved_model",
        **kwargs
    ) -> "SeanergysSklearnModel":
        """
        Restore a SeanergysSklearnModel from disk.

        Args:
            path: Path to the joblib model file produced by save().

        Returns:
            A fully restored SeanergysSklearnModel instance.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Model file not found: {path}")

        try:
            # 1. Load supplementary info
            info_path = path.parent / f"{path.stem}_info.json"
            if not info_path.exists():
                raise FileNotFoundError(
                    f"Model info file not found: {info_path}. "
                    "Cannot reconstruct the instance without it."
                )
            with open(info_path, "r") as f:
                info = json.load(f)

            # 2. Re-import the estimator class dynamically
            import importlib
            module = importlib.import_module(info["model_class_module"])
            model_class = getattr(module, info["model_class_name"])

            # 3. Reconstruct task type from stored enum name
            task_type = SeanergysModelTask[info["task_type"]]

            # 4. Build a fresh (unfitted) instance via Pydantic
            instance = cls(
                model_class=model_class,
                task_type=task_type,
                model_hyperparameters=info.get("model_hyperparameters", {}),
                **kwargs,
            )

            # 5. Replace the unfitted estimator with the persisted one
            instance.estimator = joblib.load(f"{os.path.join(str(path), cls.model_name, '.joblib')}")
            instance.is_trained = True

            # 6. Restore metadata
            metadata_path = path.parent / f"{path.stem}_metadata.json"
            if metadata_path.exists():
                with open(metadata_path, "r") as f:
                    instance.metadata = json.load(f)

            instance.logger.info(f"Model loaded from {path}")
            return instance

        except Exception as e:
            raise RuntimeError(f"Error loading model: {e}") from e

    @staticmethod
    def _to_numpy(tensor: Any) -> np.ndarray:
        """Convert a tensor or array-like object to a numpy array."""
        if isinstance(tensor, np.ndarray):
            return tensor
        if hasattr(tensor, "numpy"):
            return tensor.numpy()
        return np.array(tensor)

    def _extract_data_from_loader(
        self,
        dataloader: SeanergysDataloader,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Exhaust a dataloader and return (X, y) as concatenated numpy arrays.

        Args:
            dataloader: A SeanergysDataloader yielding (features, targets) batches.

        Returns:
            Tuple of (features, targets) numpy arrays.
        """
        features_list = [] 
        targets_list = []
        for X_batch, y_batch in dataloader:
            features_list.append(self._to_numpy(X_batch))
            targets_list.append(self._to_numpy(y_batch))

        return np.vstack(features_list), np.concatenate(targets_list)

    def get_feature_importance(self) -> Optional[np.ndarray]:
        """
        Return feature importances if the underlying estimator exposes them.

        Returns:
            Importance array (feature_importances_ or |coef_|), or None.
        """
        if hasattr(self.estimator, "feature_importances_"):
            return self.estimator.feature_importances_
        if hasattr(self.estimator, "coef_"):
            return np.abs(self.estimator.coef_)
        self.logger.warning(
            f"{self.estimator.__class__.__name__} exposes neither "
            "'feature_importances_' nor 'coef_'."
        )
        return None

    def summary(self) -> str:
        """
        Extended summary including estimator details and feature importances.
        """
        base_summary = super().summary()
        lines = [
            f"\nEstimator Class: {self.estimator.__class__.__name__}",
            f"Task Type: {self.task_type.name}",
        ]
        if self.is_trained and self.estimator is not None:
            lines.append(f"Estimator Parameters: {self.estimator.get_params()}")
            importance = self.get_feature_importance()
            if importance is not None:
                preview = importance.flat[:5]
                lines.append(
                    f"Feature Importances (first 5 of {importance.size}): {preview}"
                )
        return base_summary + "\n".join(lines)