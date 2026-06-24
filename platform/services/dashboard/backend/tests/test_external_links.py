from external_links import LinkInputs, build_links


def _inputs(**overrides):
    base = LinkInputs(
        model_name="JPCP",
        mlflow_model_id="jpcp",
        model_path_in_repo="modelzoo/modelzoo/models/tasks/power_consumption_prediction/jpcp/",
        run_id="abc123",
        version="7",
        primary_dataset="PM100Dataset",
        paper_url="https://arxiv.org/abs/1",
        public_mlflow_url="https://mlflow.example",
        public_prefect_url="https://prefect.example",
        public_ray_serve_url="https://ray.example",
        grafana_loki_explore_url="https://grafana.example/explore",
        public_control_plane_url="https://cp.example",
        examlops_repo_url="https://github.com/org/repo",
        examlops_repo_branch="main",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_all_links_built_when_inputs_complete():
    out = build_links(_inputs())
    assert out["mlflow_model"] == "https://mlflow.example/#/models/jpcp"
    assert out["mlflow_run"] == "https://mlflow.example/#/experiments/0/runs/abc123"
    assert out["ray_serve_api"] == "https://ray.example/docs#/default/predict_predict__name__post"
    assert out["prefect_flow"].startswith("https://prefect.example/")
    assert "JPCP_PM100Dataset_training_flow" in out["prefect_flow"]
    assert out["paper"] == "https://arxiv.org/abs/1"
    assert out["git_source"] == (
        "https://github.com/org/repo/blob/main/"
        "modelzoo/modelzoo/models/tasks/power_consumption_prediction/jpcp/"
    )
    assert "JPCP" in out["loki_logs"]
    assert out["control_plane_api"] == "https://cp.example/docs#/default/retrain_retrain_post"


def test_paper_omitted_when_missing():
    out = build_links(_inputs(paper_url=None))
    assert "paper" not in out


def test_git_omitted_when_repo_url_missing():
    out = build_links(_inputs(examlops_repo_url=None))
    assert "git_source" not in out


def test_run_dependent_links_omitted_when_no_run():
    out = build_links(_inputs(run_id=None))
    assert "mlflow_run" not in out
    assert "mlflow_model" in out
    assert "ray_serve_api" in out
