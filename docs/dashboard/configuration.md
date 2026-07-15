# Configuration

The dashboard's configurable values live in the Postgres `dashboard_config`
table. They are edited at runtime through the Config page (admin only).

## Catalogue

### Service endpoints (URLs — plaintext)

| Key | Default | Edited in | Used by |
|---|---|---|---|
| `mlflow_url`        | `http://localhost:5000` | Config → Endpoints | Health probe, proxy `/api/proxy/mlflow/*`, Services card |
| `prefect_url`       | `http://localhost:4200` | Config → Endpoints | Health, proxy, Services |
| `ray_serve_url`     | `http://localhost:8001` | Config → Endpoints | Health, proxy, Models page |
| `ray_dashboard_url` | `http://localhost:8265` | Config → Endpoints | Services card (deep link) |
| `prometheus_url`    | `http://localhost:9090` | Config → Endpoints | Health, proxy, Services |
| `grafana_url`       | `http://localhost:3000` | Config → Endpoints | Health, proxy (with auth injection), Services |
| `minio_url`         | `http://localhost:9000` | Config → Endpoints | Health, Services card (S3 API) |
| `minio_console_url` | `http://localhost:9001` | Config → Endpoints | Services card (Console) |
| `dataplane_sim_url` | `http://localhost:8010` | Config → Endpoints | Health, Services card |
| `seanerbus_bridge_status_url` | `$SEANERBUS_BRIDGE_STATUS_URL` env var (Docker default: `http://seanerbus-bridge:8003`) | Config → SeanerBUS | Bridge `/health` and `/stats` probe on the SeanerBUS page; when unset falls back to the env var |

### Credentials (secret — Fernet-encrypted)

| Key | Surfaced as | Used by |
|---|---|---|
| `minio_access_key` | Config → Credentials | Operator-of-record only — downstream services read MinIO credentials from their own env (`AWS_ACCESS_KEY_ID`). |
| `minio_secret_key` | Config → Credentials | As above (`AWS_SECRET_ACCESS_KEY`). |
| `grafana_api_key`  | Config → Credentials | Injected as `Authorization: Bearer <key>` for `/api/proxy/grafana/*`. |

### Promotion thresholds (plaintext, reference only)

| Key | Default | Notes |
|---|---|---|
| `threshold_jpcp_rmse` | unset | Reference value — pipeline reads thresholds from code (`pipelines/model_configs/`), not from this table. |

### Slurm adapter (plaintext — reference)

| Key | Default | Notes |
|---|---|---|
| `slurm_mode`           | unset | `mock` or `slurm`. Pipeline reads from `EXAMLOPS_SLURM_*` env vars; this table is for visibility. |
| `slurm_partition`      | unset | |
| `slurm_cpus_per_task`  | unset | |
| `slurm_mem`            | unset | |
| `slurm_time`           | unset | |

## Endpoint inventory

| Endpoint           | Port | Services card | Config field | Health | Proxied |
|---|---|---|---|---|---|
| MLflow UI          | 5000 | yes | `mlflow_url`        | yes | yes |
| Prefect UI         | 4200 | yes | `prefect_url`       | yes | yes |
| Ray Serve API      | 8001 | no  | `ray_serve_url`     | yes | yes |
| Ray Dashboard      | 8265 | yes | `ray_dashboard_url` | no  | no  |
| MinIO Console      | 9001 | yes | `minio_console_url` | no  | no  |
| MinIO S3 API       | 9000 | yes | `minio_url`         | yes | no (see [architecture](architecture.md)) |
| Prometheus         | 9090 | yes | `prometheus_url`    | yes | yes |
| Grafana            | 3000 | yes | `grafana_url`       | yes | yes (with auth injection when `grafana_api_key` set) |
| Dashboard (this)   | 8088 | n/a | n/a                 | n/a | n/a |
| Dataplane sim      | 8010 | yes | `dataplane_sim_url` | yes | no |
