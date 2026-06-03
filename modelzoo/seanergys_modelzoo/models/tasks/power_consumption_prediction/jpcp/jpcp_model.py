# Changed by Mohsen: added 'from __future__ import annotations' for Python 3.12 compatibility.
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type, Union
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import LabelEncoder
from pydantic import Field, model_validator
from sentence_transformers import SentenceTransformer
import joblib
from enum import Enum
import numpy as np
import pandas as pd

from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader
from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
from seanergys_modelzoo.datasets.f_data import FDataDataset
from seanergys_modelzoo.datasets.pm100 import PM100Dataset
from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel, SeanergysModelTask
from seanergys_modelzoo.models.common.seanergys_model_metadata import SeanergysModelMetadata
from seanergys_modelzoo.models.common.sklearn_seanergys_model import SeanergysSklearnModel

class Embedding(Enum):
    INT = 0 # Input features are mapped to categorical 
    SB = 1 # Input features are encoded with sentence_transformers
    NONE = 2 # Input features are used as is


class JPCP(SeanergysSklearnModel):
    """
    Job Power Consumption Predictor.

    A RandomForestRegressor-backed regression model that predicts the power
    consumption of HPC jobs. All SeanergysSklearnModel capabilities
    (training, prediction, save/load, feature importance) are inherited
    without modification.

    Example usage:
        model = JPCP(model_hyperparameters={"n_estimators": 200, "max_depth": 10})
        model.train(train_loader, val_loader, validation_metrics=[mean_absolute_error])
        predictions = model.predict(test_loader)
        model.save("models/jpcp.joblib")
    """    
    embedder: Optional[List] = Field(default = None, description = "The list containing the embedder instances to encode the input labels.")
    embedding_type: Optional[Embedding] = Field(default = Embedding.NONE, description = "The embedding strategy to use on the data.")
    embedding_weights: Optional[str] = Field(default ="all-MiniLM-L6-v2", description = "The weights to use for the SentenceTransformers, if embedding_type == Embdedding.SB") 
    
    @model_validator(mode="before")
    @classmethod
    def _inject_defaults(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lock in the estimator class and task type before Pydantic validation,
        while still allowing model_hyperparameters to be customised by the caller.
        """
        values.setdefault("model_class", RandomForestRegressor)
        values.setdefault("task_type", SeanergysModelTask.REGRESSION)
        values.setdefault("supported_datasets", [PM100Dataset, FDataDataset])
        values.setdefault("metadata", SeanergysModelMetadata(
                name = "JPCP", 
                created_at= datetime.now().isoformat(),
                task_type = SeanergysModelTask.REGRESSION
                ))
        
        return values
    
    @model_validator(mode='after')
    def validate_model(self) -> 'JPCP':
        """
        Implement to validate the model
        """
        return self
    
    def transform_embeddings(self, features: np.ndarray, fit:bool = False) -> np.ndarray:
        """
        Concatenate feature values into strings and encode with SentenceTransformer.

        Args:
            features: 2D array of shape (n_samples, n_features)
            encoder_weights: str containing the name of the encoder's weights, defaults to all-MiniLM-L6-v2

        Returns:
            Embedding array of shape (n_samples, embedding_dim)
        """
        if self.embedding_type == Embedding.NONE:
            return features
        elif self.embedding_type == Embedding.SB:
            self.embedder = SentenceTransformer(self.embedding_weights)
            arr = features.values if hasattr(features, "values") else features
            sentences = [" ".join(str(val) for val in row) for row in arr]
            return self.embedder.encode(sentences)
        else:
            # Changed by Mohsen: original code used numpy indexing (features[:, i]) on a
            # DataFrame, which raises InvalidIndexError, and had 'if bool' (always True,
            # shadowing the builtin). Fixed to process columns individually so mixed-type
            # DataFrames (e.g. list-valued node_power_consumption) are handled correctly.
            result = features.copy()
            self.embedder = []
            for col in features.columns:
                le = LabelEncoder()
                result[col + "_cat"] = le.fit_transform(features[col].astype(str))
                self.embedder.append(le)
            return result

    def embedding_parsing(self, embedding:np.ndarray) -> np.ndarray:
        """
        Parses the precomputed embedding vector to a suitable format for the model
        """
        
        arr = np.zeros((384,))
        arr[:] = np.array(embedding[0])
        return arr
                            
    @classmethod
    def get_train_config(cls, dataset_name:str, get_dummy_data:bool = False) -> Tuple["JPCP", SeanergysDataloader]:
        """
        Get training configuration for JPCP.

        """     
        # Dictionary containing the configurations
        if dataset_name == "f-data":       
        
            model_fdata = cls(embedding_type = Embedding.NONE, model_hyperparameters={"n_jobs": -1})
            
            model_fdata.logger.info(f"Training on FData")
            fdata = FDataDataset(
                use_zenodo_url=True,
                is_dummy = get_dummy_data,
                files = ["23_12", "24_01"],
                transform=model_fdata.embedding_parsing,
                target_transform=lambda output_features: (
                    output_features[0] / output_features[1]
                ),
                filters=[
                    ("adt", ">=", "2023-12-01"),
                    ("adt", "<=", "2024-01-31"),
                ],
                input_features=["embedding"],
                output_features=["avgpcon", "nnuma"],
            )
            data_loader_fdata = SeanergysDataloader(dataset=fdata, batch_size=1)
            return model_fdata, data_loader_fdata
        
        elif dataset_name == "pm100":
            
            model_pm100 = cls(embeding_type = Embedding.INT, model_hyperparameters={"n_jobs": -1})
            model_pm100.logger.info(f"Training on PM100")
            pm100 = PM100Dataset(
                use_zenodo_url=True,
                is_dummy = get_dummy_data,
                transform=None,
                target_transform=lambda output_features: (
                    np.mean(output_features[0]) / output_features[1]
                ),
                preprocessing_functions=[ model_pm100.transform_embeddings],
                filters=[
                    ("submit_time", ">=", pd.Timestamp("2020-05-01", tz="UTC")),
                    ("submit_time", "<=", pd.Timestamp("2020-09-01", tz="UTC")),
                ],
                columns=["num_nodes_req", "user_id", "node_power_consumption", "num_nodes_alloc"],
                input_features=["num_nodes_req_cat", "user_id_cat"],
                output_features=["node_power_consumption", "num_nodes_alloc"],
            )
            data_loader_pm100 = SeanergysDataloader(dataset=pm100, batch_size=1)
            return model_pm100, data_loader_pm100
        else:
            raise Exception("Invalid dataset name. Datasets allowed: 'f-data', 'pm100'")

            
        
    @classmethod
    def get_test_config(cls, model:SeanergysModel, dataset_name:str, get_dummy_data:bool = False) -> SeanergysDataloader:
        """
        Get testing configuration for JPCP. It requires the model and the dataset_name

        """     
        if dataset_name == "f-data":
        
            model.logger.info(f"Testing on FData")       
                    
            fdata = FDataDataset(
                use_zenodo_url=True,
                is_dummy = get_dummy_data,
                files = ["24_02"],
                transform=model.embedding_parsing,
                target_transform=lambda output_features: (
                    output_features[0] / output_features[1]
                ),
                filters=[
                    ("adt", ">=", "2024-02-01"),
                    ("adt", "<=", "2024-02-28"),
                ],
                input_features=["embedding"],
                output_features=["avgpcon", "nnuma"],
            )
            return SeanergysDataloader(dataset=fdata, batch_size=1)
        elif dataset_name == "pm100":
            # Load the fdata config
            model.logger.info(f"Testing on PM100")
            
            pm100 = PM100Dataset(
                use_zenodo_url=True,
                is_dummy = get_dummy_data,
                transform=None,
                target_transform=lambda output_features: (
                    np.mean(output_features[0]) / output_features[1]
                ),
                preprocessing_functions=[model.transform_embeddings],
                filters=[
                    ("submit_time", ">=", pd.Timestamp("2020-09-01", tz="UTC")),
                    ("submit_time", "<=", pd.Timestamp("2020-10-01", tz="UTC")),
                ],
                columns=["num_nodes_req", "user_id", "node_power_consumption", "num_nodes_alloc"],
                input_features=["num_nodes_req_cat", "user_id_cat"],
                output_features=["node_power_consumption", "num_nodes_alloc"],
            )
            return SeanergysDataloader(dataset=pm100, batch_size=1)
        else:
            raise Exception("Invalid dataset name. Datasets allowed: 'f-data', 'pm100'")
        
    
    def save(
        self,
        path: Union[str, Path] = "./saved_model",
        compress: int = 3,
        **kwargs
    ) -> bool:
        """
        Persist the estimator, embedder, metadata, and supplementary info to disk.

        Args:
            path:     Destination path for the joblib model file.
            compress: joblib compression level (0–9).

        Returns:
            True on success, False on failure.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Save the label encoder
            joblib.dump(self.embedder, f"{os.path.join(str(path), 'embedder.joblib')}", compress = compress)
            self.logger.info(f"Embedder saved to {path}")
            super().save(path=path, compress=compress)
        
        except Exception as e:
            self.logger.error(f"Error saving model: {e}")
            return False

    @classmethod
    def load(
        cls,
        path: Union[str, Path] = "./saved_model",
        **kwargs
    ) -> "JPCP":
        """
        Restore a JPCP model from disk.

        Args:
            path: Path to the joblib model file produced by save().

        Returns:
            A fully restored JPCP instance.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Model file not found: {path}")

        try:
            obj = super().load(path=path, **kwargs)
            obj.embedder = joblib.load(f"{os.path.join(str(path), 'embedder.joblib')}")
            return obj

        except Exception as e:
            raise RuntimeError(f"Error loading model: {e}") from e

    