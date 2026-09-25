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


def inject_secret_refs() -> None:
    """ADR 0011 clause 2: resolve ``secret://`` references before the agent reads its config.

    The agent image installs ``examlops`` (``pip install -e platform/cli``). When it cannot be
    imported the agent still starts — unless the environment holds a reference, which would
    otherwise be used as the literal credential.
    """
    has_refs = any(v.startswith(("secret://", "secret+file://")) for v in os.environ.values())
    try:
        from examlops.secrets.inject import inject_env
    except ImportError as exc:
        if has_refs:
            raise SystemExit(f"secret references present but examlops is unavailable: {exc}")
        return
    if has_refs:
        inject_env("agent")


def bootstrap_tracing() -> bool:
    """Install the process's OTel tracer provider before the first turn (ADR 0021 decision 2).

    Skipper's GenAI spans (``skipper.genai_trace``) go to whatever provider is global. Nothing in
    the agent installed one, so with ``OTEL_SDK_DISABLED=false`` the spans went to the API's
    no-op provider and no collector — Tempo, Langfuse or Phoenix — ever received one. This calls
    the platform's :func:`examlops.observability.setup_tracing`, which is itself a no-op while
    tracing is off. Fail-open: the platform CLI may be absent, and tracing must never keep the
    agent from starting.
    """
    try:
        # The agent image installs the platform CLI package (see the Dockerfile), so this is a
        # plain import; a checkout without it simply runs untraced.
        from examlops.observability import setup_tracing

        return bool(setup_tracing(os.environ.get("OTEL_SERVICE_NAME", "skipper")))
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: tracing not configured: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import uvicorn

    inject_secret_refs()
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
    bootstrap_tracing()
    print(f"Skipper (ExaMLOps agent)  →  http://{host}:{port}")
    if (warning := exposure_warning(host, configured_credentials or "")) is not None:
        print(warning, file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
