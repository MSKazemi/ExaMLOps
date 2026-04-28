from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple, Union, List, Callable
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from torch.utils.data import Dataset

from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger
from seanergys_modelzoo.decorators import pipeline_step

class SeanergysDataset(BaseModel, Dataset, ABC):
    """
    Base dataset class for Seanergy datasets.
    
    This class provides a foundation for creating datasets with support for
    various data sources, preprocessing, caching, and monitoring capabilities.
    """
    
    # Pydantic configuration
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra='allow'
    )
    
    # Define fields with proper Pydantic syntax
    data_path: Optional[Union[str, Path]] = Field(default=None, description="Path to data source")
    transform: Optional[Any] = Field(default=None, description="Transformations to apply to input data")
    target_transform: Optional[Any] = Field(default=None, description="Transformations to apply to targets")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Additional metadata about the dataset")
    preprocessing_functions: Optional[List[Callable]] = Field(default_factory=list, description="Preprocessing functions to apply")
    is_dummy: Optional[bool] = Field(default=False, description = "Whether to load the dataset in dummy mode. Defaults to False")
    logger: Optional[SeanergysLogger] = Field(default=None, description="Logger instance")
    stats: Dict[str, Any] = Field(
        default_factory=lambda: {
            'n_samples': 0,
            'n_features': 0,
            'load_time': 0.0,
        },
        description="Dataset statistics"
    )
    
    def model_post_init(self, __context: Any) -> None:
        """
        Pydantic v2 method called after model initialization.
        Replaces __init__ for post-initialization logic.
        """
        # Initialize logger if not provided
        if self.logger is None:
            self.logger = SeanergysLogger()
        
        if self.metadata is None:
            self.metadata = {'dataset_name': 'SeanergysDataset',}
        
        self.data_name = self.__class__.__name__
        # Log activities
        self.logger.info(f"Initialized {self.data_name}")
    
    @model_validator(mode='after')
    def validate_model(self) -> 'SeanergysDataset':
        """
        Implement to validate the model
        """
        return self
    
    @abstractmethod
    def __getitem__(self, idx: int) -> Tuple[Any, Any]:
        """
        Retrieve a single item from the dataset.
        
        Args:
            idx: Index of the item to retrieve
            
        Returns:
            Tuple of (data, target)
        """
        pass
        
    @abstractmethod
    def __len__(self) -> int:
        """Return the total number of samples in the dataset."""
        pass
            
    def get_stats(self) -> Dict[str, Any]:
        """Return dataset statistics."""
        return self.stats.copy()
    
    def get_metadata(self) -> Dict[str, Any]:
        """Return dataset metadata."""
        return {
            **self.metadata,
            "stats": self.stats,
        }

    # ── Pipeline step interface ────────────────────────────────────────────
    # Subclasses that implement load_data() get this hook for free.

    @pipeline_step(name="load_step")
    def load_step(self) -> "SeanergysDataset":
        """Pipeline entry point: triggers data loading and returns self."""
        if hasattr(self, "load_data") and callable(self.load_data):
            self.load_data()
        return self