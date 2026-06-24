# JupyterHub Notebooks

ExaMLOps ships a JupyterHub environment so researchers from different institutions can run notebooks with direct access to the full production stack — MLflow, MinIO, Ray Serve, Prefect, and the Control Plane.

Each user who logs in gets a **dedicated JupyterLab container** spawned automatically on login. That container joins the `examlops_default` Docker network, so all internal service hostnames work without any manual configuration.

## Starting JupyterHub

```bash
make jupyter-up
```

Open **http://localhost:18888** (or **http://<DATAPLANE_HOST>:18888** on remote server `remote-cpu01`, or use `ssh remote` port-forward).

## First-Time Login (Admin Account)

JupyterHub uses `NativeAuthenticator` — there is no pre-set password. On first use:

1. Open http://localhost:18888
2. Click **Sign up** and register with username **`admin`**  and any password you choose
3. Click **Login** — because `admin` is in `admin_users`, your account is auto-approved
4. You are now inside your personal JupyterLab

> **Note:** With `open_signup = False`, accounts created by non-admin users are not auto-approved. The admin must approve them via the admin panel (see [User Management](#user-management)).

---

## Pre-configured Environment Variables

Every user container automatically has these env vars set — no configuration needed:

| Variable | Value |
|---|---|
| `MLFLOW_TRACKING_URI` | `http://mlflow:5000` |
| `MLFLOW_S3_ENDPOINT_URL` | `http://minio:9000` |
| `AWS_ACCESS_KEY_ID` | *(MinIO root user from stack config)* |
| `AWS_SECRET_ACCESS_KEY` | *(MinIO root password from stack config)* |
| `PREFECT_API_URL` | `http://orchestrator:4200/api` |
| `RAY_SERVE_URL` | `http://ray-serving:8001` |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` |

Verify them in a notebook cell:

```python
import os

for var in [
    "MLFLOW_TRACKING_URI", "MLFLOW_S3_ENDPOINT_URL",
    "PREFECT_API_URL", "RAY_SERVE_URL", "CONTROL_PLANE_URL",
]:
    print(f"{var} = {os.environ.get(var, 'NOT SET')}")
```

---

## Connecting to Services

### MLflow — Experiment Tracking

```python
import mlflow
import os

# Tracking URI is already set — but mlflow reads it from the env automatically
# so this line is optional
mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])

client = mlflow.tracking.MlflowClient()

# List all registered models
for rm in client.search_registered_models():
    print(rm.name)
    for v in rm.latest_versions:
        print(f"  version={v.version}  stage={v.current_stage}  aliases={v.aliases}")
```

```python
# Search runs in a specific experiment
runs = client.search_runs(
    experiment_ids=["1"],   # find experiment IDs at http://mlflow:5000
    order_by=["metrics.val_loss ASC"],
    max_results=10,
)
for r in runs:
    print(r.info.run_id, r.data.metrics)
```

```python
# Log a new run from a notebook
with mlflow.start_run(experiment_id="1", run_name="notebook-test"):
    mlflow.log_param("learning_rate", 0.01)
    mlflow.log_metric("accuracy", 0.95)
    print("Run logged:", mlflow.active_run().info.run_id)
```

MLflow UI is also reachable in the browser at **http://localhost:15000** (host) or **http://<DATAPLANE_HOST>:15000** (remote-cpu01).

---

### MinIO — Artifact Storage

```python
import boto3
import os

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)

# List buckets
for b in s3.list_buckets()["Buckets"]:
    print(b["Name"])

# List objects in the MLflow artifacts bucket
for obj in s3.list_objects_v2(Bucket="mlflow-artifacts").get("Contents", []):
    print(obj["Key"], f"  {obj['Size']} bytes")

# Download a specific artifact
s3.download_file("mlflow-artifacts", "path/to/model.pkl", "/tmp/model.pkl")
```

MinIO Console (browser UI): **http://localhost:19001** — login with `minioadmin` / `minioadmin` (default).

---

### Ray Serve — Model Inference

```python
import requests
import os

base = os.environ["RAY_SERVE_URL"]

# Health check
print(requests.get(f"{base}/health").json())

# List available models
print(requests.get(f"{base}/models").json())

# Single prediction (Production alias of JPCP model)
resp = requests.post(
    f"{base}/predict/jpcp",
    json={"features": {"feature_0": 1.2, "feature_1": 0.8, "feature_2": -0.3}},
)
print(resp.status_code, resp.json())

# Prediction against a specific alias
resp = requests.post(
    f"{base}/predict/jpcp",
    json={"features": {"feature_0": 1.2}},
    params={"alias": "Canary"},
)
print(resp.json())
```

Ray Dashboard: **http://localhost:18265** (local) / **http://<DATAPLANE_HOST>:18265** (remote-cpu01).

---

### Prefect — Pipeline Orchestration

```python
from prefect.client.orchestration import get_client
import asyncio

async def list_flows():
    async with get_client() as client:
        flows = await client.read_flows()
        for f in flows:
            print(f.name, f.id)

asyncio.run(list_flows())
```

```python
# Trigger a pipeline run programmatically
async def trigger_run(deployment_name: str):
    async with get_client() as client:
        deployments = await client.read_deployments()
        dep = next(d for d in deployments if d.name == deployment_name)
        flow_run = await client.create_flow_run_from_deployment(dep.id)
        print("Flow run created:", flow_run.id)

asyncio.run(trigger_run("jpcp-nightly"))
```

Prefect UI: **http://localhost:14200** (local) / **http://<DATAPLANE_HOST>:14200** (remote-cpu01).

---

### Control Plane — Trigger Retraining

```python
import requests
import os

url = os.environ["CONTROL_PLANE_URL"]

# Trigger a retrain job (requires CONTROL_PLANE_TOKEN)
import os
token = os.environ.get("CONTROL_PLANE_TOKEN", "")   # set this if using auth

resp = requests.post(
    f"{url}/retrain",
    json={"model": "JPCP", "dataset": "PM100Dataset", "dummy": True},
    headers={"Authorization": f"Bearer {token}"} if token else {},
)
print(resp.status_code, resp.json())

# Check approval queue
resp = requests.get(f"{url}/approvals/pending")
print(resp.json())
```

---

## File Persistence

| Location | Persists across restarts? | Notes |
|---|---|---|
| `/home/jovyan/work/` | **Yes** — Docker volume `jupyter-user-{username}` | Your notebooks and personal files |
| `/home/jovyan/` (outside `work/`) | **No** — lost on container restart | Temporary installs, scratch files |
| `/tmp/` | **No** | Truly ephemeral |

**Rule:** Keep all notebooks and data you care about inside `~/work/`.

---

## Installing Additional Packages

Packages installed at runtime survive only until your container is stopped:

```bash
# In a notebook cell (persists until container restart)
!pip install scikit-optimize lightgbm
```

For a permanent install, ask the admin to add the package to `Dockerfile.jupyterlab` and rebuild:

```bash
make jupyter-up   # rebuilds the image and restarts
```

---

## VS Code Connectivity

1. Install the **Jupyter** extension in VS Code
2. Command Palette (`Ctrl+Shift+P`) → **Jupyter: Specify Jupyter Server for Connections**
3. Select **Existing**
4. Enter: `http://<host>:18888/user/<your-username>/?token=<token>`

Your token: log in → click your name (top right) → **Token** → **Request new API token**.

---

## User Management (Admin)

### Add a user

```bash
# Generate an admin token first at http://localhost:18888/hub/token
make jupyter-add-user USER=alice HUB_TOKEN=<admin-token>
```

Then set the new user's password via **http://localhost:18888/hub/admin** → find the user → **Edit**.

### Remove a user

```bash
curl -X DELETE http://localhost:18888/hub/api/users/<username> \
  -H "Authorization: token <admin-token>"
```

The user's home volume (`jupyter-user-<username>`) is **not** deleted automatically — run `docker volume rm jupyter-user-<username>` to free disk space.

### Admin panel

**http://localhost:18888/hub/admin** — start, stop, and inspect any user's server.

---

## Operations Reference

| Command | Purpose |
|---|---|
| `make jupyter-up` | Build images + start JupyterHub (port 18888) |
| `make jupyter-down` | Stop JupyterHub (user volumes preserved) |
| `make jupyter-logs` | Tail Hub container logs |
| `make jupyter-add-user USER=x HUB_TOKEN=y` | Add a user via REST API |

---

## Troubleshooting

### "Server failed to start" after login

Your per-user container failed to launch. Check Hub logs:

```bash
make jupyter-logs
```

Common causes:
- **`examlops-jupyterlab` image not built** — run `make jupyter-up` again, it rebuilds the image
- **Port conflict** — another process on port 18888; check with `ss -tlnp | grep 18888`
- **Docker socket permission** — Hub needs `/var/run/docker.sock`; verify with `docker ps`

### Services return connection errors from inside a notebook

The full stack must be running:

```bash
make stack-up
```

Without it, the internal hostnames (`mlflow`, `minio`, etc.) do not resolve even though the env vars are set.

### Lost my token

Log in → click username (top right) → **Token** → revoke old tokens → **Request new API token**.

### My notebooks disappeared

Files outside `~/work/` are lost on container restart. In future, save everything to `~/work/`.

---

## Ports Quick Reference

| Service | Local | Remote (remote-cpu01) |
|---|---|---|
| JupyterHub | http://localhost:18888 | http://<DATAPLANE_HOST>:18888 |
| MLflow | http://localhost:15000 | http://<DATAPLANE_HOST>:15000 |
| MinIO Console | http://localhost:19001 | http://<DATAPLANE_HOST>:19001 |
| Prefect UI | http://localhost:14200 | http://<DATAPLANE_HOST>:14200 |
| Ray Dashboard | http://localhost:18265 | http://<CONTROL_PLANE_HOST>:18265 |
| Grafana | http://localhost:13000 | http://<CONTROL_PLANE_HOST>:13000 |
