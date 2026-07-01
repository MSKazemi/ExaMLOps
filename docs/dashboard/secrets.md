# Secrets

## What is encrypted

Three secret keys live in the dashboard `dashboard_config` table:

- `minio_access_key`
- `minio_secret_key`
- `grafana_api_key`

Their values are stored in the `secret_value` column as Fernet ciphertext
(AES-128-CBC + HMAC-SHA256). The plaintext never leaves the backend except
when injected into a proxied request (Grafana).

## How keys move

```
Admin types value in UI
        │
        ▼
PUT /api/config  {grafana_api_key: "g-key"}
        │
        ▼   secret_store.encrypt("g-key")
        │
        ▼
Postgres dashboard_config.secret_value (bytea, Fernet token)
        │
        ▼   secret_store.decrypt(...) on read
        │
        ▼
Used by inject_grafana_bearer in routers/proxy.py
```

## The Fernet key

`DASHBOARD_SECRET_KEY` is a 44-character base64-urlsafe Fernet key.

Generate one:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Lose this key and the stored secrets are unrecoverable.** They can only be
re-entered through the UI by an admin. The backend fails fast on startup if
the key is unset, so you'll find out immediately rather than at the first
secret write.

## Masking on read

`GET /api/config` returns `"***"` for any secret key that has a value, and
`null` for unset secret keys. Plaintext is never returned to the browser.

## PUT semantics for secret keys

| User intent                         | Send in JSON body | Backend action |
|---|---|---|
| Leave as-is                         | omit key entirely | nothing |
| Type a new value                    | string non-empty  | encrypt + store; audit `set` |
| Clear the secret                    | `null`            | NULL the column; audit `clear` |
| Anything else (e.g. blank `""`)     | rejected          | `422 Unprocessable Entity` |

## Manual key rotation

`DASHBOARD_SECRET_KEY` rotation is **out of scope** for this iteration.
If you must rotate (e.g. suspected key leak):

1. As admin, log in and **clear** all three secret keys via the Config UI
   (the × button). Verify with `GET /api/config` that each shows `null`.
2. Stop the dashboard.
3. Replace `DASHBOARD_SECRET_KEY` in `.env` with a freshly generated Fernet key.
4. Start the dashboard.
5. As admin, re-enter the three secret values via the Config UI.

Do not skip step 1. If you change the Fernet key while ciphertext rows still
exist, every read of those rows raises `cryptography.fernet.InvalidToken` and
the affected proxied requests will fail.

## Audit

Every secret write produces a row in `dashboard_audit`:

| column | content |
|---|---|
| `id` | autoincrement |
| `at` | timestamp UTC |
| `role` | always `admin` (only role that writes) |
| `action` | `set` / `clear` |
| `key` | which config key was touched |

No values — neither plaintext nor ciphertext — are stored in the audit table.

Read recent rows from the UI (admin → Audit page) or the API:

```bash
curl -H "Authorization: Bearer $JWT" http://localhost:8088/api/audit?limit=50
```
