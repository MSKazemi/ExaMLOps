from __future__ import annotations

import os
import sys

# Put platform/services/agent on sys.path so `import exa_agent` works.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    import uvicorn
    from exa_agent.server import app  # noqa: F401

    port = int(os.environ.get("AGENT_SERVER_PORT", "18004"))
    host = os.environ.get("AGENT_SERVER_HOST", "0.0.0.0")
    print(f"ExaMLOps Agent Chat  →  http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
