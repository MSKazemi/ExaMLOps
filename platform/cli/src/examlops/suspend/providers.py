"""Register the ``suspend_backend`` provider domain (ADR 0109 decision 5, ADR 0074).

Importing this module registers the built-ins. Third-party backends load from the
``exa.providers.suspend_backend`` entry-point group; the default is ``checkpoint-only``.
"""

from __future__ import annotations

from examlops.providers import register_provider

from .backends import CheckpointOnlyBackend, MockBackend

DOMAIN = "suspend_backend"

register_provider(DOMAIN, "checkpoint-only", CheckpointOnlyBackend, default=True)
register_provider(DOMAIN, "mock", MockBackend)
