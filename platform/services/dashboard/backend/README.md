# Dashboard backend

FastAPI service that serves the React SPA and a small JSON API for the
ExaMLOps platform.

## Layout

```
backend/
├── auth.py            JWT issue/verify, password compare, role gate
├── database.py        Async SQLAlchemy engine, session factory, init_db()
├── main.py            FastAPI app, router wiring, SPA fallback
├── models.py          DashboardConfig + DashboardAudit ORM
├── secret_store.py    Fernet encrypt/decrypt facade (the ONLY caller of
│                      cryptography.fernet)
├── settings.py        Pydantic-settings; env-var contract
├── routers/
│   ├── auth.py        /login /logout /me
│   ├── audit.py       /audit (admin)
│   ├── config.py      /config /config/keys (mask + encrypt + audit)
│   ├── docs.py        /docs/tree /docs/content
│   ├── health.py      /health (open)
│   └── proxy.py       /proxy/{svc}/{path} with per-service auth injection
└── tests/             pytest-asyncio + httpx ASGITransport, sqlite in-memory
```

## Run locally

From the repo root:

```bash
cd services/dashboard/backend
pip install -r requirements.txt
export DATABASE_URL=sqlite+aiosqlite:///:memory:
export DASHBOARD_VIEWER_PASSWORD=v
export DASHBOARD_ADMIN_PASSWORD=a
export DASHBOARD_JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export DASHBOARD_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uvicorn main:app --reload --port 8099
```

## Tests

```bash
pytest -v
```

## Documentation

Operator-facing docs live in `docs/dashboard/` (gitignored — local mkdocs build
or human reading only).
