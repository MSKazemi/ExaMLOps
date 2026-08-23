# Operations runbook

## First-time bootstrap

1. **Generate the auth secrets:**

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"   # JWT secret
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # Fernet key
   ```

2. **Copy `.env.example` → `.env`** and fill in:

   ```ini
   DASHBOARD_VIEWER_PASSWORD=...
   DASHBOARD_ADMIN_PASSWORD=...
   DASHBOARD_JWT_SECRET=...
   DASHBOARD_SECRET_KEY=...
   ```

3. **Bring up the stack:** `make stack-up`. The dashboard creates its tables and
   seeds the three secret-key rows on first boot.

4. **Sign in** at http://localhost:18099 — first with the viewer password to
   verify the read path, then with the admin password.

5. As admin, **set the Grafana API key** (Config → Credentials). Confirm by
   loading a Grafana panel via `/api/proxy/grafana/...` (or the Models page if
   it routes through Grafana).

## Running it without Docker

For frontend or backend work, run the two halves directly. The backend needs the same four
secrets as the container; an in-memory database keeps the run throwaway.

```bash
cd platform/services/dashboard/backend
export DATABASE_URL=sqlite+aiosqlite:///:memory:
export DASHBOARD_VIEWER_PASSWORD=v DASHBOARD_ADMIN_PASSWORD=a
export DASHBOARD_JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export DASHBOARD_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uvicorn main:app --reload --port 8099
```

```bash
# second terminal
cd platform/services/dashboard/frontend && npm install && npm run dev
```

Point `PLATFORM_DB` at the same `platform.db` the CLI uses if you want the consoles to show real
platform state; otherwise they render their empty states. Schema migrations for the dashboard's
own tables are Alembic's: `cd platform/services/dashboard/backend && alembic upgrade head`.

## Backup

What to back up, in order of how much pain you're in if you lose it:

1. `DASHBOARD_SECRET_KEY` — without this, every encrypted row in
   `dashboard_config` is unrecoverable.
2. `DASHBOARD_JWT_SECRET` — if lost, every active JWT becomes invalid; users
   re-log in. Generate a new one and restart.
3. The Postgres `mlflow` database — contains `dashboard_config` (URLs +
   ciphertext) and `dashboard_audit`. The compose stack uses a named volume
   (`postgres_data`); back it up with `docker run --rm -v
   postgres_data:/data alpine tar czf - /data > backup.tar.gz` or your usual
   Postgres dump.
4. `DASHBOARD_VIEWER_PASSWORD` / `DASHBOARD_ADMIN_PASSWORD` — kept by whoever
   needs to log in.

## Smoke checklist (run after any release)

- [ ] `make stack-up` brings the dashboard up; `/api/health` returns 200.
- [ ] Login with viewer password → `role: viewer` in JWT, Save button hidden
      on the Config page, "Read-only" banner visible.
- [ ] Login with admin password → role chip says `admin`, Audit link appears
      in side nav.
- [ ] Edit `mlflow_url`, hit Save → page reloads with the new value.
- [ ] Set `grafana_api_key` → audit page shows a `set` row.
- [ ] Click the Clear (×) button on `grafana_api_key` → audit page shows a
      `clear` row, GET returns `null` for that key.
- [ ] `docker compose restart dashboard` → secrets that were set survive
      (because they're in Postgres, not env).
- [ ] Boot with `DASHBOARD_SECRET_KEY` blank in `.env` → dashboard exits
      non-zero with a clear error message; `docker compose logs dashboard`
      shows the validation failure.

## Incident playbooks

### Lost `DASHBOARD_JWT_SECRET`

Impact: every issued JWT is invalidated. Active sessions get 401 and re-prompt.

1. Generate a new value: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Update `.env`.
3. `docker compose up -d dashboard` to pick up the change.

No data loss. Users sign in again.

### Lost `DASHBOARD_SECRET_KEY`

Impact: every row in `dashboard_config` with `is_secret=true` and a non-NULL
`secret_value` is unrecoverable.

1. Update `.env` with a fresh key.
2. `docker compose up -d dashboard`.
3. Connect to Postgres and clear the affected rows:

   ```sql
   UPDATE dashboard_config
   SET secret_value = NULL
   WHERE is_secret = true;
   ```

   (The dashboard would otherwise raise `InvalidToken` on every read.)
4. As admin, re-enter the three secret values via the Config UI.

The audit log will record the new `set` rows.

### Migrating from the legacy `DASHBOARD_TOKEN`

Pre-existing dashboards used a single `DASHBOARD_TOKEN` env var. After this
release:

1. Replace `DASHBOARD_TOKEN` with the four new vars from the [Auth
   contract](auth.md).
2. Generate `DASHBOARD_SECRET_KEY` (see Bootstrap above).
3. Restart. The first boot creates the new tables and seeds the secret-key
   rows; existing URL rows in `dashboard_config` carry over unchanged
   (`is_secret=false` is the default).
