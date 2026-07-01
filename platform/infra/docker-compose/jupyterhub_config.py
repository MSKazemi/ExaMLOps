import os

# ── Authentication ─────────────────────────────────────────────────────────
# NativeAuthenticator stores usernames + hashed passwords in the Hub SQLite DB.
# open_signup=False means only admin can create accounts (via admin panel or API).
c.JupyterHub.authenticator_class = "nativeauthenticator.NativeAuthenticator"
c.NativeAuthenticator.open_signup = False
c.JupyterHub.admin_users = {"admin"}

# ── Spawner ────────────────────────────────────────────────────────────────
# Each login spawns a dedicated Docker container from examlops-jupyterlab.
c.JupyterHub.spawner_class = "dockerspawner.DockerSpawner"
c.DockerSpawner.image = "examlops-jupyterlab"
c.DockerSpawner.network_name = os.environ.get("DOCKER_NETWORK_NAME", "examlops_default")

# Per-user home dir → named Docker volume (created on first login).
c.DockerSpawner.volumes = {"jupyter-user-{username}": "/home/jovyan/work"}

# ── Stack environment injected into every user container ───────────────────
# Internal Docker hostnames work because user containers join examlops_default.
c.DockerSpawner.environment = {
    "MLFLOW_TRACKING_URI":    "http://mlflow:5000",
    "MLFLOW_S3_ENDPOINT_URL": "http://minio:9000",
    "AWS_ACCESS_KEY_ID":      os.environ.get("MINIO_ROOT_USER", "minioadmin"),
    "AWS_SECRET_ACCESS_KEY":  os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
    "PREFECT_API_URL":        "http://orchestrator:4200/api",
    "RAY_SERVE_URL":          "http://ray-serving:8001",
    "CONTROL_PLANE_URL":      "http://control-plane:8002",
}

# ── Hub networking ─────────────────────────────────────────────────────────
# hub_ip=0.0.0.0 → Hub listens on all interfaces inside the container.
# hub_connect_ip → hostname spawned containers use to reach the Hub;
#                  must match the container name on examlops_default.
c.JupyterHub.hub_ip = "0.0.0.0"
c.JupyterHub.hub_connect_ip = "examlops-jupyterhub"

# ── State persistence ──────────────────────────────────────────────────────
# Both paths are on the jupyter_hub_data Docker volume mounted at /data.
c.JupyterHub.db_url = "sqlite:////data/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = "/data/jupyterhub_cookie_secret"
