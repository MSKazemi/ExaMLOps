from __future__ import annotations

import os
import sys

# Put platform/services/agent on sys.path so `import exa_agent` works without
# touching the `platform.` import path (which shadows the stdlib platform module).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exa_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
