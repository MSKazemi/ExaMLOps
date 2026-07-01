# Authentication

## Two-password model

The dashboard recognises exactly two roles:

| Role | Password env var | Can do |
|---|---|---|
| `viewer` | `DASHBOARD_VIEWER_PASSWORD` | Read everything; secret values appear as `***`. |
| `admin`  | `DASHBOARD_ADMIN_PASSWORD`  | Read + edit URLs + write/rotate secrets + read audit log. |

There is no user table. Anyone with the password has the role.

## JWT lifecycle

- `POST /api/auth/login` exchanges a password for a JWT.
- The JWT is HS256-signed with `DASHBOARD_JWT_SECRET`.
- TTL: `DASHBOARD_JWT_TTL_HOURS` hours (default 12).
- Stored in browser `localStorage` under key `dashboard_auth`.
- No silent refresh: when the token expires, the next API call returns 401 and the SPA re-prompts.

## Role matrix

| Route | Required role |
|---|---|
| `POST /api/auth/login` | none |
| `POST /api/auth/logout` | viewer |
| `GET  /api/auth/me` | viewer |
| `GET  /api/health` | none (probe-friendly) |
| `GET  /api/config` | viewer (secrets masked) |
| `GET  /api/config/keys` | viewer (metadata only) |
| `PUT  /api/config` | admin |
| `GET  /api/audit` | admin |
| `GET  /api/proxy/{svc}/{path}` | viewer |
| `POST/PUT/DELETE/PATCH /api/proxy/{svc}/{path}` | admin |

## Env-var contract

| Var | Required | Default | Notes |
|---|---|---|---|
| `DASHBOARD_VIEWER_PASSWORD` | yes | — | |
| `DASHBOARD_ADMIN_PASSWORD`  | yes | — | |
| `DASHBOARD_JWT_SECRET`      | yes | — | ≥ 32 random bytes |
| `DASHBOARD_JWT_TTL_HOURS`   | no  | 12 | |
| `DASHBOARD_SECRET_KEY`      | yes | — | Fernet key (see [Secrets](secrets.md)) |

The legacy `DASHBOARD_TOKEN` is removed. Update `.env` before redeploying.

## OpenAPI

The full API reference is auto-generated at `/api/docs` on a running dashboard.
