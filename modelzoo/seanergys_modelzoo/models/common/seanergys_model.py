from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Optional, Union, List, Literal, Tuple, Callable
from pathlib import Path
import json
import pickle
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, model_validator

from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger
from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader
from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
from seanergys_modelzoo.models.common.seanergys_model_metadata import SeanergysModelMetadata
from seanergys_modelzoo.decorators import pipeline_step


class SeanergysModelTask(Enum):
    CLASSIFICATION = 1
    REGRESSION = 2


class SeanergysModel(BaseModel, ABC):
    """
    Base model class for Seanergys models.
    
    Provides standard interface for training, prediction, saving, and loading models
    with built-in support for metrics tracking and model versioning.
    
    This is a framework-agnostic base class that can be extended for PyTorch,
    TensorFlow, scikit-learn, or any other ML framework.
    """
    
    # Pydantic configuration
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra='allow'
    )
    
    # Define fields with proper Pydantic syntax
    task_type: SeanergysModelTask = Field(..., description="Type of task - regression or classification")
    supported_datasets: Optional[List] = Field(description="List of SeanergysDatasets the model can work with", default = [])
    is_trained: Optional[bool] = Field(default=False, description="Training state flag")
    logger: Optional[SeanergysLogger] = Field(default_factory=SeanergysLogger, description="Logger instance for tracking")
    metadata: Optional[SeanergysModelMetadata|dict] = Field(default_factory = SeanergysModelMetadata, description="SeanergysModelMetadata instance storing the metadata for the model")
    
    def model_post_init(self, __context: Any) -> None:
        """
        Pydantic v2 method called after model initialization.
        Replaces __init__ for post-initialization logic.
        """
        self.model_name = self.__class__.__name__
        
        # Initialize metadata
        if not self.metadata:
            
            self.metadata = SeanergysModelMetadata(
                name = self.model_name, 
                created_at= datetime.now().isoformat(),
                task_type = self.task_type
                )
            
           
        else:
            if isinstance(self.metadata, dict):
                self.metadata = SeanergysModelMetadata.from_dict(self.metadata)
        # Initialize logger if not provided
        if self.logger is None:
            self.logger = SeanergysLogger()
        
        # Log activities
        self.logger.info(f"Initialized {self.__class__.__name__}")
    
    @abstractmethod
    def build_model(self) -> None:
        """
        Build the model architecture.
        
        This method should define all model components and architecture.
        Must be implemented by subclasses.
        """
        return self
    
    @model_validator(mode='after')
    def validate_model(self) -> 'SeanergysModel':
        """
        Implement to validate the model
        """
        pass
    
    @abstractmethod
    def train(
        self,
        train_data_loader: SeanergysDataloader,
        val_data_loader: Optional[SeanergysDataloader] = None,
        training_loss: Optional[Callable] = None,
        validation_metrics: Optional[List[Callable]] = None,
        **kwargs
    ) -> Dict[str, List[float]]:
        """
        Train the model.
        
        Args:
            train_data_loader: A SeanergysDataloader containing training data
            val_data_loader: SeanergysDataloader containing validation data (optional)
            training_loss: Loss function for training
            validation_metrics: List of metric functions for validation
            **kwargs: Additional training arguments (epochs, batch_size, etc.)
            
        Returns:
            Dictionary containing training history/metrics
            
        Note:
            Subclasses should implement framework-specific training logic.
            Should update self.training_history and self.metadata.
        """
        pass
    
    @abstractmethod
    def predict(
        self,
        data_loader: SeanergysDataloader,
        **kwargs
    ) -> Iterable:
        """
        Make predictions on input data.
        
        Args:
            data_loader: A SeanergysDataloader containing input data for prediction
            **kwargs: Additional prediction arguments
            
        Returns:
            Predictions (format depends on framework and task)
            
        Note:
            Subclasses should implement framework-specific prediction logic.
        """
        pass

    @abstractmethod
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
            metrics: List of the metrics to use to evaluate the model. If None the subclasses should implement at least one by default.
            **kwargs: Additional prediction arguments
        Returns:
            List[Score] (format depends on framework and task and metrics given in input)
            
        Note:
            Subclasses should implement framework-specific evaluation logic.
        """
        pass

    
    @abstractmethod
    def save(
        self,
        path: Union[str, Path],
        **kwargs
    ) -> bool:
        """
        Save model to disk.
        
        Args:
            path: Path to save the model
            **kwargs: Additional save options (e.g., save_optimizer, save_format)
            
        Note:
            Subclasses should implement framework-specific save logic.
            Should save model weights/parameters, config, metadata, and training history.
        """
        pass
    
    @classmethod
    @abstractmethod
    def load(
        cls,
        path: Union[str, Path],
        **kwargs
    ) -> 'SeanergysModel':
        """
        Load model from disk.
        
        Args:
            path: Path to load the model from
            **kwargs: Additional load options
            
        Returns:
            Loaded model instance
            
        Note:
            Subclasses should implement framework-specific load logic.
            Should restore model weights/parameters, config, metadata, and training history.
        """
        pass
    
    @classmethod
    def get_train_config(cls, dataset_name:str, get_dummy_data:bool = False) -> Tuple[SeanergysModel, SeanergysDataloader]:
        pass
    
    @classmethod
    def get_test_config(cls, model:SeanergysModel, dataset_name:str, get_dummy_data:bool = False) -> SeanergysDataloader:
        pass
            
    def get_metadata(self) -> Dict[str, Any]:
        """
        Get model metadata.
        
        Returns:
            Model metadata dictionary
        """
        return self.metadata.copy()
    
    def get_training_history(self) -> Dict[str, List[float]]:
        """
        Get training history.
        
        Returns:
            Training history dictionary
        """
        return self.training_history.copy()
            
    def _save_metadata(self, path: Union[str, Path]) -> None:
        """
        Save model metadata to JSON file.
        
        Args:
            path: Base path for saving (metadata will be saved as path_metadata.json)
        """
        path = Path(path)
        metadata_path = path.parent / f"{path.stem}_metadata.json"
        
        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=2)
        
        self.logger.info(f"Metadata saved to {metadata_path}")
        
    # ── Pipeline step interface ────────────────────────────────────────────
    # Concrete wrappers around the abstract ML methods.
    # Subclasses only need to implement train / evaluate / predict.
    # These step methods are inherited automatically and marked for
    # orchestrator auto-discovery via @pipeline_step.

    @pipeline_step(name="train_step", retries=1)
    def train_step(
        self,
        train_data_loader: SeanergysDataloader,
        val_data_loader: Optional[SeanergysDataloader] = None,
    ) -> Dict[str, List[float]]:
        """Pipeline entry point: delegates to self.train()."""
        return self.train(train_data_loader, val_data_loader)

    @pipeline_step(name="evaluate_step")
    def evaluate_step(
        self,
        data_loader: SeanergysDataloader,
        metrics: Optional[List[Callable]] = None,
    ) -> List:
        """Pipeline entry point: delegates to self.evaluate()."""
        return self.evaluate(data_loader, metrics)

    @pipeline_step(name="predict_step")
    def predict_step(
        self,
        data_loader: SeanergysDataloader,
    ) -> Iterable:
        """Pipeline entry point: delegates to self.predict()."""
        return self.predict(data_loader)

    def __repr__(self) -> str:
        """
        String representation of the model.
        
        Returns:
            Model description string
        """
        return self.__class__.__name__
        
    def summary(self) -> str:
        """
        Get a summary of the model.
        
        Returns:
            Multi-line string with model information
            
        Note:
            Subclasses can override this to provide framework-specific summaries
        """
        summary_lines = [
            f"Model: {self.model_name}",
            f"Created: {self.metadata.get('created_at', 'unknown')}",
            f"Is Trained: {self.is_trained}",
        ]
        
        return "\n".join(summary_lines)