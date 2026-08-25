from __future__ import annotations

import os
import sys

# Put platform/services/agent on sys.path so `import skipper` works.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def exposure_warning(host: str, configured_credentials: str) -> str | None:
    """Warn when an operator deliberately exposes the agent beyond loopback."""
    if host in _LOOPBACK:
        return None
    if configured_credentials:
        return (
            f"WARNING: binding {host} exposes the agent to the network. Credential auth protects "
            "chat, history, and WebSocket routes, but the token must be transported over TLS. "
            "Prefer a TLS reverse proxy or bind AGENT_SERVER_HOST=127.0.0.1 and use a secure tunnel."
        )
    return (
        f"WARNING: binding {host} without agent credentials exposes chat, tools, and conversation "
        "history without authentication. Set AGENT_API_KEY or AGENT_API_KEYS_JSON, or bind "
        "AGENT_SERVER_HOST=127.0.0.1 and use a secure tunnel."
    )


if __name__ == "__main__":
    import uvicorn
    from skipper.server import app  # noqa: F401

    port = int(os.environ.get("AGENT_SERVER_PORT", "18004"))
    host = os.environ.get("AGENT_SERVER_HOST", "127.0.0.1")
    require_key = os.environ.get("AGENT_REQUIRE_API_KEY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    configured_credentials = os.environ.get("AGENT_API_KEY") or os.environ.get(
        "AGENT_API_KEYS_JSON"
    )
    if require_key and not configured_credentials:
        raise SystemExit(
            "AGENT_API_KEY or AGENT_API_KEYS_JSON is required when AGENT_REQUIRE_API_KEY=true"
        )
    print(f"Skipper (ExaMLOps agent)  →  http://{host}:{port}")
    if (warning := exposure_warning(host, configured_credentials or "")) is not None:
        print(warning, file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
