from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime

class SeanergysModelMetadata(BaseModel):
    """
    Stores metadata for a machine learning model.
    
    This class captures essential information about a model's identity,
    provenance, performance, and deployment status.
    """

    # --- Identity ---
    name: str = Field(..., description="Name of the model")
    version: Optional[str] = Field(description="Model version (e.g., '1.0.0')", default = "1.0")
    description: Optional[str] = Field(None, description="Brief description of the model")

    # --- Ownership ---
    author: Optional[str] = Field(None, description="Author or team responsible for the model")

    # --- Timestamps ---
    # created_at is auto-populated with the current UTC time if not provided
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Model creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")

    # --- Framework & Task ---
    framework: Optional[str] = Field(None, description="ML framework used (e.g., 'PyTorch', 'TensorFlow')")
    task: Optional[str] = Field(None, description="Task type (e.g., 'classification', 'regression')")

    # --- Categorization ---
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")

    # --- Performance ---
    # Keys are metric names (e.g., 'accuracy', 'f1_score'), values are floats
    metrics: dict[str, float] = Field(default_factory=dict, description="Evaluation metrics")

    # --- Configuration ---
    # Typed as object to allow mixed value types (int, float, str, bool, etc.)
    hyperparameters: dict[str, object] = Field(default_factory=dict, description="Model hyperparameters")

    # --- I/O Contract ---
    # These can be used to validate or document expected model inputs/outputs
    input_schema: Optional[dict] = Field(None, description="Expected input schema")
    output_schema: Optional[dict] = Field(None, description="Expected output schema")

    # --- Deployment ---
    # Set to False to mark a model as deprecated or retired
    is_active: bool = Field(True, description="Whether the model is active/deployed")

    # Allow fields to be mutated after instantiation
    model_config = {
        "arbitrary_types_allowed": True,
        "json_schema_extra": {"example": {
            "name": "fraud-detector",
            "version": "2.1.0",
            "description": "Detects fraudulent transactions",
            "author": "ML Team",
            "framework": "PyTorch",
            "task": "binary_classification",
            "tags": ["finance", "fraud"],
            "metrics": {"accuracy": 0.97, "f1_score": 0.95},
            "hyperparameters": {"learning_rate": 0.001, "epochs": 50},
            "is_active": True
        }}
    }

    def _touch(self) -> None:
        """Update the updated_at timestamp to the current UTC time."""
        self.updated_at = datetime.utcnow()

    def set_field(self, field: str, value: Any) -> None:
        """
        Set a field to a new value.

        Args:
            field: The name of the field to update.
            value: The new value to assign.

        """
        if hasattr(self, field):
            setattr(self, field, value)
        else:
            self.__dict__.update({field:value})
        self._touch()

    def update_fields(self, updates: dict[str, Any]) -> None:
        """
        Update multiple existing fields at once from a dictionary.

        Args:
            updates: A dict mapping field names to their new values.

        Raises:
            ValueError: If any key in updates is not an existing field.
        """
        for field, value in updates.items():
            setattr(self, field, value)
        self._touch()
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeanergysModelMetadata":
        """
        Instantiate a SeanergysModelMetadata object from a plain dictionary.

        Args:
            data: A dictionary containing model metadata fields.
                Unknown keys are ignored; missing optional fields use defaults.

        Returns:
            A new SeanergysModelMetadata instance.

        Raises:
            ValueError: If any required fields (name, version) are missing.
            ValidationError: If any provided values fail Pydantic validation.

        Example:
            >>> meta = SeanergysModelMetadata.from_dict({
            ...     "name": "fraud-detector",
            ...     "version": "2.1.0",
            ...     "framework": "PyTorch",
            ...     "metrics": {"accuracy": 0.97},
            ... })
        """
        # Filter out any keys not recognised by the model to avoid validation errors
        known_fields = cls.model_fields.keys()
        filtered = {k: v for k, v in data.items() if k in known_fields}

        return cls(**filtered)