# Dashboard

The ExaMLOps dashboard is a single FastAPI service (`platform/services/dashboard/`) that
serves a React SPA and a small JSON API. It surfaces the platform's other
components (MLflow, Prefect, Ray, MinIO, Grafana, Prometheus, the dataplane
simulator) through deep links, health checks, and a reverse proxy.

**URL:** http://localhost:18099

## Pages

| Page | Role | Description |
|---|---|---|
| **Overview** | viewer | Platform health at a glance — service status badges, quick links |
| **Models** | viewer/admin | All registered models with lifecycle stage badges (Staging / Canary / Production / Archived) and ModelZoo freshness badges (CURRENT / UPDATED); recent ModelZoo push event feed; admin "New Model" button opens ScaffoldWizard |
| **Model Detail** | viewer/admin | Per-model README, stage badges, try-it-out inference form, image gallery, admin markdown editor, drift banner |
| **Services** | viewer/admin | Start / stop / restart stack services, live status badges, tail Docker logs |
| **Pipelines** | viewer/admin | Prefect deployment status, recent run history, admin trigger button per deployment |
| **Datasets** | viewer | Dataset list from the configured model zoo (GitLab) |
| **Docs** | viewer | Rendered repo documentation |
| **Config** | admin | Service URLs, Grafana API key, GitLab token, encrypted secrets; ModelZoo Integration section with webhook URL and auto-retrain toggle |
| **Approvals** | admin | Pending model change approvals from CI — approve to fire Prefect training or reject with optional reason |
| **Audit** | admin | Chronological config write history (values never recorded) |

## Topics

- [Architecture](architecture.md) — components, data flow, threat model.
- [Authentication](auth.md) — viewer/admin shared passwords, JWT lifecycle, role matrix.
- [Secrets](secrets.md) — how credentials are stored, masked, and rotated.
- [Configuration](configuration.md) — every config key, where it surfaces, who edits it.
- [Operations](operations.md) — bootstrap, backup, incident playbooks.

## API reference

The OpenAPI spec is auto-generated and available at
`http://localhost:18099/api/docs` once the service is running.
