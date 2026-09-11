"""Container entrypoint for the dataplane service (ADR 0130)."""

import os

import uvicorn

from examlops.dataplane.service.app import configure_logging, create_app

if __name__ == "__main__":
    # examlops at INFO (the startup auth-mode line, pull failures); every other library — the
    # HTTP clients that log full URLs above all — stays at WARNING.
    configure_logging()
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("DATAPLANE_PORT", "8010")))  # noqa: S104
