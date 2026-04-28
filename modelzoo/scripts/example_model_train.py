from seanergys_modelzoo.models.tasks.power_consumption_prediction.jpcp.jpcp_model import JPCP
from sklearn.metrics import mean_squared_error

if __name__ == "__main__":

    # Load model and data loader for training
    train_config_dict = JPCP.get_train_config("f-data", get_dummy_data=True)
    model_obj, dataloader = train_config_dict

    # Train the model
    model_obj.train(dataloader)

    # Get testing data 
    test_dataloader = JPCP.get_test_config(model=model_obj, dataset_name="f-data", get_dummy_data=True)
    results = model_obj.evaluate(test_dataloader, metrics = [mean_squared_error])
    print(results)