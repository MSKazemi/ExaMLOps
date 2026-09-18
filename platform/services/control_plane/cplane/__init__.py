"""The control plane's modules (plan P1.1).

`app.py` stays the composition root: it builds the FastAPI app, wires routes and background
workers, and re-exports every name defined here, so callers and tests that reach
`app.<name>` keep working while the code lives in focused modules:

- `schema`   — the control plane's own tables, indices and additive column migrations
- `models`   — request/response models of the HTTP API
- `gateway`  — the Prefect client: pooled httpx, per-dispatch deadline, circuit breaker
- `problems` — RFC 9457 problem documents for /v1
"""
