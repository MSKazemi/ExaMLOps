# Architecture

## Components

```
┌─────────────────────────────────────────────────────────────────────────────────── ────┐
│ Browser                                                                               │
│ React SPA (Vite build, served by FastAPI as static files)                             │
│ - Auth: JWT in localStorage                                                           │
│ - Pages: Overview / Services / Models / Pipelines / Approvals / Config / Audit / Docs │
└──────────────┬────────────────────────────────────────────────────────────────────────┘
               │ HTTPS (operator's reverse proxy) → HTTP on 8088
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ FastAPI backend  (platform/services/dashboard/backend/)            │
│ ┌──────────────────────────────────────────────────────────────┐   │
│ │ routers/                                                     │   │
│ │   auth.py    POST /login (open)  /me  /logout                │   │
│ │   health.py  GET /health (open)                              │   │
│ │   config.py  GET (viewer, masked) PUT (admin, encrypts)      │   │
│ │   audit.py   GET (admin)                                     │   │
│ │   proxy.py   {svc}/{path} (viewer for safe / admin for unsafe│   │
│ │              + per-service auth injection)                   │   │
│ │              services: mlflow, prefect, ray, prometheus,     │   │
│ │              grafana, control_plane (Phase 12)               │   │
│ │   docs.py        /tree /content (viewer)                     │   │
│ │   approvals.py   GET /approvals, POST /approve/{id}          │   │
│ │                  POST /reject/{id} (admin) — proxies to CP   │   │
│ │   containers.py service controls (start/stop/restart,logtail)│   │
│ │   pipelines.py   GET /deployments /runs  POST /trigger       │   │
│ │                  (viewer/admin) — Prefect REST proxy         │   │
│ │   scaffold.py    POST /preview /create (admin)               │   │
│ │                  runs scaffold_model.py in container         │   │
│ │ secret_store.py   Fernet encrypt/decrypt facade              │   │
│ │ auth.py           JWT issue/verify, role gate                │   │
│ └──────────────────────────────────────────────────────────────┘   │
└──────────────┬─────────────────────────────────────────────────────┘
               │ asyncpg                                       │
               ▼                                               ▼
        ┌──────────────────────────┐         Upstream services
        │ Postgres                 │         MLflow / Prefect /
        │  dashboard_config        │         Ray / Prometheus /
        │  dashboard_audit         │         Grafana / MinIO /
        │  (Fernet ciphertext      │         Dataplane sim /
        │   in secret_value)       │         Control Plane (Phase 12)
        └──────────────────────────┘
```

## Sequence: login

```
Browser                                Backend
  │ POST /api/auth/login {password}      │
  │─────────────────────────────────────▶│
  │                            check_password() (constant-time)
  │                            issue_token(role)
  │              200 {token, role, expires_at}
  │◀─────────────────────────────────────│
  │ localStorage.setItem('dashboard_auth', ...)
```

## Sequence: secret write (admin)

```
Browser                            Backend                     Postgres
  │ PUT /api/config                  │                            │
  │  {grafana_api_key: "g-key"}      │                            │
  │ Authorization: Bearer <jwt>      │                            │
  │─────────────────────────────────▶│                            │
  │                                  │ verify_token() → role=admin
  │                                  │ encrypt("g-key")           │
  │                                  │ UPDATE dashboard_config    │
  │                                  │   SET secret_value=...     │
  │                                  │─────────────────────────────▶
  │                                  │ INSERT dashboard_audit     │
  │                                  │   (admin, set, grafana_api_key)
  │                                  │─────────────────────────────▶
  │           200 {grafana_api_key: "***", ...}
  │◀─────────────────────────────────│
```

## Sequence: proxied request with auth injection

```
Browser                       Backend (proxy.py)          Grafana
  │ GET /api/proxy/grafana/api/dashboards
  │ Authorization: Bearer <jwt>      │                      │
  │─────────────────────────────────▶│                      │
  │                  verify_token() → role=viewer ok        │
  │                  fetch grafana_api_key, decrypt()       │
  │                  inject Authorization: Bearer g-key     │
  │                                  │ GET /api/dashboards  │
  │                                  │─────────────────────▶│
  │                                  │     200 [...]        │
  │                                  │◀─────────────────────│
  │           200 [...]              │                      │
  │◀─────────────────────────────────│                      │
```

## Threat model


| Attack                             | Mitigation in scope                                                     | Out of scope                                                        |
| ---------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Stolen JWT (XSS)                   | None inherent; no third-party scripts in SPA. Future: httpOnly cookies. | Browser-side XSS hardening beyond what React provides.              |
| DB dump leaked alone               | Fernet ciphertext is opaque without the key.                            | Re-keying secrets after a leak — manual via re-entry.               |
| DB dump + env file leaked together | Approach 3 (Vault) — not implemented; flagged as future work.           | —                                                                   |
| Brute-force login                  | None at this layer.                                                     | Rate limiting, fail2ban-style throttles — operator's reverse proxy. |
| Lost `DASHBOARD_SECRET_KEY`        | Fail-fast on boot; documented re-entry procedure.                       | Automatic recovery (impossible by design).                          |


