from typing import Any, Callable, Dict, List, Optional, Tuple
from sklearn.metrics import (
    mean_squared_error,
    mean_absolute_error,
    r2_score,
    accuracy_score,
    f1_score
)

from seanergys_modelzoo.logger.seanergys_logger import SeanergysLogger
from seanergys_modelzoo.models.common.seanergys_model import SeanergysModel, SeanergysModelTask
from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader

class SeanergysModelEvaluator:
    """
    Base class for evaluating ML models, datasets, and configurations in MLOps projects.
    
    This class orchestrates the validation of three core components:
    - Model class
    - Dataset class
    - Configuration file
    
    It supports train, evaluate, and predict operations to ensure all components
    work correctly together.
    """
    
    def __init__(
        self,
        model_class: SeanergysModel,
        logger: SeanergysLogger
    ):
        """
        Initialize the evaluator with model, dataset classes and configuration.
        
        Args:
            model_class: SeanergysModel class
        """
        self.model= model_class
        
        self._logger = logger
                
        self._logger.info("SeanergysModelEvaluator initialized successfully")
                
    def run_train(self, train_data_loader:SeanergysDataloader, val_data_loader:Optional[SeanergysDataloader] = None, **kwargs) -> Any:
        """
        Execute model training operation.
        
        Args:
            train_data_loader:SeanergysDataloader the data loader instance containing the training data.
            val_data_loader:Optional[SeanergysDataloader] the data loader instance containing the validation data.
            **kwargs: Additional arguments to pass to model.train()
            
        Returns:
            Training results/metrics
        """
        if self.model is None or train_data_loader is None:
            raise RuntimeError("Instantiate model and train_data_loader before calling run_train()")
        
        self._logger.info("Starting training...")
        
        try:
            results = self.model.train(
                train_data_loader,
                val_data_loader,
                **kwargs
            )
            self._logger.info("Training completed successfully")
            return results
            
        except Exception as e:
            self._logger.error(f"Training failed: {e}")
            return None
        
    def run_predict(self, data:SeanergysDataloader, saved_model_path:Optional[str] = None, **kwargs) -> Any:
        """
        Execute model prediction operation.
        
        Args:
            data: Input data for prediction
            saved_model_path:Optional[str] path to the saved model. If provided the model load will be called. If 'None' the model will be used as is.
            **kwargs: Additional arguments to pass to model.predict()
            
        Returns:
            Model predictions
        """
        if self.model is None or data is None:
            raise RuntimeError("Instantiate model and data before calling run_predict()")
        
        self._logger.info("Starting prediction...")
        
        try:
            predictions = self.model.predict(data, **kwargs)
            self._logger.info("Prediction completed successfully")
            return predictions
            
        except Exception as e:
            self._logger.error(f"Prediction failed: {e}")
            return []
    
    def validate_model(self, train_data_loader:SeanergysDataloader, val_data_loader:SeanergysDataloader, predict_data_loader:SeanergysDataloader, save_model_path:str) -> Dict[str, bool]:
        """
        Validate the model functioning
        
        Returns:
            Dictionary with validation status for each operation
        """
        self._logger.info("Starting component validation...")
        results = {
            'training': False,
            'validation': False,
            'prediction': False,
            "load": False,
            "save": False
        }
        
        try:         
            training_loss = mean_squared_error if self.model._task_type == SeanergysModelTask.REGRESSION else accuracy_score
            validation_metrics = [mean_squared_error, mean_absolute_error, r2_score] if self.model._task_type == SeanergysModelTask.REGRESSION else [accuracy_score, f1_score]
            training_results = self.run_train(train_data_loader=train_data_loader, val_data_loader=val_data_loader, training_loss=training_loss, val_metrics=validation_metrics)
            
            # Check if results are valid
            if training_results:
                
                if "training_results" in training_results:
                    results['training'] = True
                    for k in training_results["training_results"]:
                        if not(training_results["training_results"][k]):
                            results['training'] = False
                
                if "validation_results" in training_results:
                    results['validation'] = True
                    for k in training_results["validation_results"]:
                        if not(training_results["validation_results"][k]):
                            results['validation'] = False
                                    
            # Test prediction
            predictions = self.run_predict(predict_data_loader)
            if predictions:
                results['prediction'] = True
            
            # Test save model
            save_res = self.model.save(save_model_path)
            if save_res:
                results['save'] = True
            
            # Test load model 
            model = self.model.load(save_model_path)
            if model:
                results['load'] = True
            
            self._logger.info("All component validations passed!")
        except Exception as e:
            self._logger.error(f"Validation failed: {e}")
            raise
        
        return results