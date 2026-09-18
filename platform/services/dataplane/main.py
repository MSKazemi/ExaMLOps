"""Container entrypoint for the dataplane service (ADR 0130)."""

import os

import uvicorn

from examlops.dataplane.service.app import configure_logging, create_app, drain_seconds

if __name__ == "__main__":
    # examlops at INFO (the startup auth-mode line, pull failures); every other library — the
    # HTTP clients that log full URLs above all — stays at WARNING.
    configure_logging()
    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104
        port=int(os.getenv("DATAPLANE_PORT", "8010")),
        # E10 (ADR 0131): in-flight HTTP gets the same drain budget the lifespan drain counts
        # down from — one deadline, set at SIGTERM — so shutdown never takes twice the budget.
        timeout_graceful_shutdown=int(drain_seconds()),
    )
