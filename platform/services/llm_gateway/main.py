"""Container entrypoint for the LLM gateway service (ADR 0151)."""

import logging
import os

import uvicorn

from examlops.gateway.service.app import create_app
from examlops.secrets.inject import inject_env

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LLM_GATEWAY_LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines carry full URLs
    # ADR 0011 clause 2: resolve `secret://` references before any credential is read;
    # an unresolvable one refuses to start. A no-op when there is none.
    inject_env("llm-gateway")
    uvicorn.run(
        create_app(),
        host=os.getenv("LLM_GATEWAY_HOST", "0.0.0.0"),  # noqa: S104 - the container's own interface
        port=int(os.getenv("LLM_GATEWAY_PORT", "8020")),
        timeout_graceful_shutdown=int(os.getenv("LLM_GATEWAY_DRAIN_SECONDS", "30")),
    )
