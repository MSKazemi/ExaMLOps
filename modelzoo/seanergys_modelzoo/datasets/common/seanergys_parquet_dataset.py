from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union
from pathlib import Path
import time

import torch
import pandas as pd
from pydantic import Field, model_validator

from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger


class SeanergysParquetDataset(SeanergysDataset):
    """
    Dataset class for loading tabular data from Parquet files.

    Supports loading from both local filesystem and remote URLs with
    configurable input/output features and filtering capabilities.
    """

    # Pydantic fields
    input_features: List[str] = Field(..., description="List of column names to use as input features")
    output_features: List[str] = Field(..., description="List of column names to use as output/target features")
    columns: Optional[List[str]] = Field(default = None, description="List of column names to use from the original dataset. If None is input_features + output_features")
    filters: Optional[Union[List[Tuple], List[List[Tuple]]]] = Field(
        default=None,
        description=(
            "Filter syntax: [[(column, op, val), ...], ...] "
            "where op is [==, =, >, >=, <, <=, !=, in, not in]. "
            "Inner tuples are ANDed; outer list is ORed."
        )
    )

    # Private attribute — not a Pydantic field
    _df: Optional[pd.DataFrame] = None

    @model_validator(mode='after')
    def validate_model(self) -> 'SeanergysParquetDataset':
        """Validate and trigger data loading after model initialization."""
        if self.columns is None:
            self.columns = self.input_features + self.output_features
        self.load_data()
        return self

    def load_data(self) -> None:
        """
        Load data from the specified Parquet file and apply preprocessing.
        """
        if self.data_path is None:
            raise ValueError("data_path must be provided to load data.")

        start_time = time.time()

        self.logger.info(f"Loading Parquet data from: {self.data_path}")

        read_kwargs = self.model_extra or {}
        self._df = pd.read_parquet(
            self.data_path,
            filters=self.filters,
            **read_kwargs
        )

        # Apply preprocessing functions: each entry is (columns, function)
        for columns, fn in (self.preprocessing_functions or []):
            self._df[columns] = fn(self._df[columns])

        elapsed = time.time() - start_time

        self.stats.update({
            'n_samples': len(self._df),
            'n_features': len(self.input_features) + len(self.output_features),
            'load_time': elapsed,
        })

        self.logger.info(
            f"Loaded {self.stats['n_samples']} samples in {elapsed:.3f}s"
        )
        
    def __len__(self) -> int:
        """Return the total number of samples in the dataset."""
        if self._df is None:
            return 0
        return len(self._df)

    def __getitem__(self, idx: int) -> Tuple[Any, Any]:
        """
        Retrieve a single item from the dataset.

        Args:
            idx: Index of the item to retrieve

        Returns:
            Tuple of (input_tensor, target_tensor)
        """
        if self._df is None:
            raise RuntimeError("Data not loaded. Call load_data() first.")

        row = self._df.iloc[idx]

        x = torch.tensor(row[self.input_features].values.astype(float), dtype=torch.float32)
        y = torch.tensor(row[self.output_features].values.astype(float), dtype=torch.float32)

        if self.transform is not None:
            x = self.transform(x)
        if self.target_transform is not None:
            y = self.target_transform(y)

        return x, y

    def get_feature_statistics(self) -> Dict[str, Dict[str, float]]:
        """
        Compute basic statistics for input and output features.

        Returns:
            Dictionary with statistics for each feature.
        """
        if self._df is None:
            raise RuntimeError("Data not loaded. Call load_data() first.")

        return {
            feature: {
                'mean':   float(self._df[feature].mean()),
                'std':    float(self._df[feature].std()),
                'min':    float(self._df[feature].min()),
                'max':    float(self._df[feature].max()),
                'median': float(self._df[feature].median()),
            }
            for feature in self.input_features + self.output_features
        }

    def __repr__(self) -> str:
        """String representation of the dataset."""
        return (
            f"{self.__class__.__name__}("
            f"n_samples={self.stats.get('n_samples', 0)}, "
            f"n_input_features={len(self.input_features)}, "
            f"n_output_features={len(self.output_features)}, "
            f"n_filters={len(self.filters) if self.filters else 0})"
        )