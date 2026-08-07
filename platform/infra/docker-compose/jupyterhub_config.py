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
# Remove a workbench's container when its server stops (the per-server *volume* persists), so a
# stopped/deleted workbench leaves no lingering Exited container.
c.DockerSpawner.remove = True

# Per-user home dir → named Docker volume (created on first login). Plus the platform repo,
# bind-mounted read-only at /repo so a notebook can `import examlops` for plugin/provider
# management (ADR 0074); the authored-providers dir is mounted read-write so notebook-authored
# providers are shared with the CLI/dashboard/serving. DockerSpawner spawns *sibling* containers
# via the Docker socket, so these are HOST paths (EXAMLOPS_HOST_REPO on the deploy node).
_HOST_REPO = os.environ.get("EXAMLOPS_HOST_REPO", "/opt/examlops")
c.DockerSpawner.volumes = {
    # {servername} is empty for the default lab and the workbench name for a named server, so each
    # project workbench gets its own persistent home volume.
    "jupyter-user-{username}-{servername}": "/home/jovyan/work",
    _HOST_REPO: {"bind": "/repo", "mode": "ro"},
    f"{_HOST_REPO}/.providers": {"bind": "/repo/.providers", "mode": "rw"},
    # Read-WRITE so a notebook can author/update pipeline code and have the platform pick it up:
    #   usecases/  → model YAML + per-model configs + dataset schemas (the pipeline definitions, ADR 0094)
    #   pipelines/ → the Prefect pipeline engine / generator
    # Edits land on the deploy node's repo (git-tracked); commit + push via the p2p pipeline to ship.
    # Everything else under /repo stays read-only (import-safe, can't accidentally break platform code).
    f"{_HOST_REPO}/usecases": {"bind": "/repo/usecases", "mode": "rw"},
    f"{_HOST_REPO}/pipelines": {"bind": "/repo/pipelines", "mode": "rw"},
}

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
    # Make `import examlops` work in notebooks (dependency-free providers pkg) and share
    # authored plugins with the rest of the platform (ADR 0074).
    "PYTHONPATH":             "/repo/platform/cli/src",
    "EXAMLOPS_PROVIDERS_DIR": "/repo/.providers",
    "EXAMLOPS_USECASE_DIR":   "/repo/usecases/seanergy",
    "PLATFORM_DB":            "/repo/platform.db",
}


# ── Per-project shared volume ("PV/PVC — volume per project") ──────────────────
# Every workbench in a project mounts one shared Docker volume at /project, so ALL of a
# project's workbenches share a persistent disk — distinct from the per-workbench home
# (/home/jovyan/work) and the per-project MinIO bucket. The named-server id is "<project>-<name>".
#
# The reserved "platform-ops" project is the governed platform-management workbench (M3): it also
# gets a shared config dir (so notebook config/finops/policy writes reach the platform) and — for a
# Hub admin — rw mounts of the integration source (Tier B). That wiring lives in the testable helper
# examlops.workbench_spawn.platform_ops_spawn (imported lazily so a missing PYTHONPATH never breaks
# a normal spawn).
def _pre_spawn(spawner):
    server = spawner.name or ""
    project = server.rsplit("-", 1)[0] if "-" in server else (server or "default")
    spawner.volumes[f"examlops-project-{project}-shared"] = "/project"
    spawner.environment = {
        **spawner.environment,
        "EXAMLOPS_PROJECT": project,
        "PROJECT_SHARED_DIR": "/project",
        # Attribute façade audit rows to the real Hub user, not the container root.
        "EXAMLOPS_ACTOR": getattr(spawner.user, "name", "") or "unknown",
    }
    try:
        from examlops.workbench_spawn import platform_ops_spawn

        extra_vols, extra_env = platform_ops_spawn(
            project,
            is_admin=bool(getattr(spawner.user, "admin", False)),
            host_repo=_HOST_REPO,
            actor=getattr(spawner.user, "name", None),
        )
        spawner.volumes.update(extra_vols)
        spawner.environment = {**spawner.environment, **extra_env}
    except Exception as exc:  # never let platform-ops wiring break a normal workbench spawn
        spawner.log.warning("platform_ops_spawn wiring skipped: %s", exc)


c.Spawner.pre_spawn_hook = _pre_spawn

# ── Named servers = project workbenches (spawned by the dashboard via the Hub API) ─────────
# A "workbench" (ADR 0090) maps to a JupyterHub named server: the dashboard's docker-socket-proxy
# forbids container creation, so JupyterHub (which holds real Docker access) is the spawner, and it
# also proxies HTTP+WebSocket under port 18888 so kernels work through the existing tunnel.
c.JupyterHub.allow_named_servers = True
c.JupyterHub.named_server_limit_per_user = 10

# Privileged service token the dashboard backend uses to start/stop a user's workbench servers via
# the Hub REST API (in-cluster at http://examlops-jupyterhub:8000 — not through the socket-proxy).
_dash_token = os.environ.get("JUPYTERHUB_DASHBOARD_TOKEN")
if _dash_token:
    c.JupyterHub.services = [{"name": "dashboard", "api_token": _dash_token}]
    c.JupyterHub.load_roles = [
        {
            "name": "dashboard-workbench-spawner",
            "services": ["dashboard"],
            "scopes": ["admin:servers", "admin:users", "list:users", "read:users"],
        }
    ]

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
