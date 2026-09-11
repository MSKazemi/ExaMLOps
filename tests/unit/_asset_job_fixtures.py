"""Importable production functions for the scheduler-orchestrator tests.

A scheduler job imports its production function by ``module:qualname``, so these must live at
module level. Import this module inside a test (never at collection time): the ``@asset`` below
writes its declaration to whichever ``PLATFORM_DB`` is current when it is imported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from examlops.assets import asset


def build_marker(**upstream):
    """Record which process built the asset, and with which upstream versions."""
    Path(os.environ["EXAMLOPS_TEST_ASSET_MARKER"]).write_text(
        json.dumps({"pid": os.getpid(), "upstream": upstream})
    )


def build_fails(**upstream):
    raise RuntimeError("partition 7 is corrupt")


@asset(name="job_fixture_decorated", kind="feature")
def decorated(**upstream):
    build_marker(**upstream)
