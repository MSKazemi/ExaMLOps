from __future__ import annotations

import os
import sys

# Put platform/services/agent on sys.path so `import skipper` works.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def exposure_warning(host: str, api_key: str) -> str | None:
    """Warn when an operator deliberately exposes the agent beyond loopback."""
    if host in _LOOPBACK:
        return None
    if api_key:
        return (
            f"WARNING: binding {host} exposes the agent to the network. AGENT_API_KEY protects "
            "chat, history, and WebSocket routes, but the token must be transported over TLS. "
            "Prefer a TLS reverse proxy or bind AGENT_SERVER_HOST=127.0.0.1 and use a secure tunnel."
        )
    return (
        f"WARNING: binding {host} with AGENT_API_KEY unset exposes chat, tools, and conversation "
        "history without authentication. Set AGENT_API_KEY, or bind "
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
    if require_key and not os.environ.get("AGENT_API_KEY"):
        raise SystemExit("AGENT_API_KEY is required when AGENT_REQUIRE_API_KEY=true")
    print(f"Skipper (ExaMLOps agent)  →  http://{host}:{port}")
    if (warning := exposure_warning(host, os.environ.get("AGENT_API_KEY", ""))) is not None:
        print(warning, file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
