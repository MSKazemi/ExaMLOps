"""JPCP as a pipeline-as-code definition (ADR 0080) — the DSL twin of ``models/jpcp.yaml``.

    exa pipeline compile examples/pipeline-as-code/jpcp_flow.py --out jpcp.ir.json
    exa pipeline explain jpcp.ir.json
    exa pipeline run --ir jpcp.ir.json --dummy

This file is trusted-tier Python: ``exa pipeline compile`` executes it. Nothing in the body runs a
training job — the helpers only record steps.
"""

from examlops.sdk import dataset, evaluate, pipeline, promote, train


@pipeline(
    name="JPCP",
    dataplane_bus_uuid="30b0f24c-e154-432b-91f9-25a144095a30",
    serving={"model_id": "jpcp", "aliases": ["Production", "Canary", "Staging"]},
    prefect={
        "schedule": "0 2 * * *",
        "deployment_name": "examlops-jpcp-nightly",
        "work_pool": "default-agent",
        "concurrency_limit": 1,
    },
    inference={
        "input_schema": {"embedding": "list[float]"},
        "output_schema": {"power_per_node_watts": "float"},
    },
)
def jpcp():
    pm100 = dataset(
        "PM100Dataset",
        backend="zenodo",
        cache_dir=".data_cache/pm100",
        batch_size=1,
        columns=["num_nodes_req", "user_id", "node_power_consumption", "num_nodes_alloc"],
        input_features=["num_nodes_req_cat", "user_id_cat"],
        output_features=["node_power_consumption", "num_nodes_alloc"],
        splits={
            "train": {
                "filters": [
                    ["submit_time", ">=", "2020-05-01T00:00:00+00:00"],
                    ["submit_time", "<=", "2020-09-01T00:00:00+00:00"],
                ]
            },
            "validation": {
                "filters": [
                    ["submit_time", ">=", "2020-09-01T00:00:00+00:00"],
                    ["submit_time", "<=", "2020-10-01T00:00:00+00:00"],
                ]
            },
        },
    )
    fdata = dataset(
        "FDataDataset",
        backend="zenodo",
        cache_dir=".data_cache/fdata",
        batch_size=1,
        input_features=["embedding"],
        feature_view="fdata_job_features",  # ADR 0017: the one train/serve definition
        output_features=["avgpcon", "nnuma"],
        splits={
            "train": {
                "files": ["23_12", "24_01"],
                "filters": [["adt", ">=", "2023-12-01"], ["adt", "<=", "2024-01-31"]],
            },
            "validation": {
                "files": ["24_02"],
                "filters": [["adt", ">=", "2024-02-01"], ["adt", "<=", "2024-02-28"]],
            },
        },
    )
    model = train(
        pm100,
        fdata,
        model_class="JPCP",
        config_class="jpcp_config.JPCPConfiguration",
        task_type="regression",
        framework="sklearn",
        model={"embedding_type": "INT", "hyperparameters": {"n_jobs": -1}},
    )
    metrics = evaluate(model)
    promote(
        metrics,
        lifecycle=[
            {
                "name": "Staging",
                "metric": "rmse",
                "threshold": 200.0,
                "direction": "lower_is_better",
            },
            {
                "name": "Canary",
                "metric": "rmse",
                "threshold": 100.0,
                "direction": "lower_is_better",
            },
            {
                "name": "Production",
                "metric": "rmse",
                "threshold": 50.0,
                "direction": "lower_is_better",
            },
        ],
    )
