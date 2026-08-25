from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Internal upstream URLs (Docker network) ──
    mlflow_url: str = "http://localhost:15000"
    prefect_url: str = "http://localhost:14200"
    ray_serve_url: str = "http://localhost:18001"
    ray_dashboard_url: str = "http://localhost:18265"
    prometheus_url: str = "http://localhost:19090"
    grafana_url: str = "http://localhost:13000"
    minio_url: str = "http://localhost:19000"
    minio_console_url: str = "http://localhost:19001"
    control_plane_url: str = "http://localhost:18002"
    agent_url: str = "http://localhost:18004"
    agent_api_key: str = ""
    jupyterhub_url: str = "http://localhost:18888"
    loki_url: str = "http://localhost:13100"
    seanerbus_bridge_status_url: str = "http://localhost:8003"

    # ── Browser-facing URLs ──
    public_mlflow_url: str = "http://localhost:15000"
    public_prefect_url: str = "http://localhost:14200"
    public_ray_dashboard_url: str = "http://localhost:18265"
    public_prometheus_url: str = "http://localhost:19090"
    public_grafana_url: str = "http://localhost:13000"
    public_ray_serve_url: str = "http://localhost:18001"
    public_minio_console_url: str = "http://localhost:19001"
    public_control_plane_url: str = "http://localhost:18002"
    public_jupyterhub_url: str = "http://localhost:18888"
    public_loki_url: str = "http://localhost:13100"
    public_seanerbus_bridge_url: str = "http://localhost:18003"
    public_dashboard_url: str = "http://localhost:18099"
    slurm_mode: str = "mock"

    # ── Database ──
    database_url: str = "postgresql+asyncpg://mlops:mlops@localhost/mlflow"

    # ── MinIO image storage (model description images) ──
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    dashboard_minio_bucket: str = "dashboard-model-docs"
    dashboard_max_image_bytes: int = 5 * 1024 * 1024
    dashboard_image_url_ttl_seconds: int = 300

    # ── External resource linking (per-model detail page) ──
    examlops_repo_url: str | None = None
    examlops_repo_branch: str = "main"
    grafana_loki_explore_url: str = "http://localhost:13000/explore"

    # ── Auth (REQUIRED — no defaults; backend fails fast if unset) ──
    dashboard_viewer_password: str = Field(...)
    dashboard_admin_password: str = Field(...)
    dashboard_jwt_secret: str = Field(...)
    dashboard_jwt_ttl_hours: int = 12

    # ── GitLab ModelZoo integration ──
    gitlab_url: str = "https://gitlab.com"
    gitlab_branch: str = "main"
    gitlab_token: str | None = None
    gitlab_project_id: str | None = None

    # ── AI-Production pipeline trigger (fallback for "Run CI Pipeline" button) ──
    ai_prod_pipeline_trigger_token: str | None = None
    ai_prod_gitlab_project_id: str | None = None

    # ── Secrets at rest (REQUIRED) ──
    dashboard_secret_key: str = Field(...)

    # ── Dashboard runtime ──
    dashboard_port: int = 8099
    env_export_path: str = "/app/.env.dashboard"

    # ── Scaffold (model generation) ──
    repo_root: str | None = None

    # Shared platform SQLite database (written by CLI and SeanerBUS bridge)
    platform_db: str = "/repo/platform.db"


# Required fields are supplied from the environment at runtime by
# pydantic-settings; mypy can't see that, so it flags them as missing.
settings = Settings()  # type: ignore[call-arg]
