from __future__ import annotations

import os
import sys

# Put platform/services/agent on sys.path so `import skipper` works.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def exposure_warning(host: str, api_key: str) -> str | None:
    """Warn when the agent is reachable off-box without a token.

    ``AGENT_API_KEY`` gates ``POST /v1/chat/completions`` and nothing else — the WebSocket chat at
    ``/ws/chat/{thread_id}`` and the thread-history endpoints are open to anyone who can reach the
    port. The default bind is ``0.0.0.0``, so the default posture on a shared host is an
    unauthenticated agent that can call tools. Setting a key does not close that; only the bind
    address does, until the native endpoints are gated too.
    """
    if host in _LOOPBACK:
        return None
    return (
        f"WARNING: binding {host} — the WebSocket chat and thread history are reachable from the "
        "network and are NOT gated by AGENT_API_KEY"
        + (" (which is set, but only covers /v1/chat/completions)." if api_key else " (unset).")
        + " Bind AGENT_SERVER_HOST=127.0.0.1 and reach it over an SSH tunnel unless this host is "
        "already on a trusted network."
    )


if __name__ == "__main__":
    import uvicorn
    from skipper.server import app  # noqa: F401

    port = int(os.environ.get("AGENT_SERVER_PORT", "18004"))
    host = os.environ.get("AGENT_SERVER_HOST", "0.0.0.0")
    print(f"Skipper (ExaMLOps agent)  →  http://{host}:{port}")
    if (warning := exposure_warning(host, os.environ.get("AGENT_API_KEY", ""))) is not None:
        print(warning, file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
