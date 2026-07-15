"""
Lightweight markers for pipeline integration points.

No Prefect (or any orchestrator) dependency. These decorators simply tag
methods as pipeline steps so orchestrators can discover and wrap them.

Usage in a model or dataset subclass:

    class MyModel(SeanergysSklearnModel):
        @pipeline_step(name="ingest", retries=2)
        def ingest_step(self, raw_path: str) -> None:
            ...

Attributes attached to the decorated function:
    _is_pipeline_step  bool  — always True
    _step_name         str   — canonical step name (default: function __name__)
    _step_retries      int   — retry hint for the orchestrator
    _step_cache        bool  — whether the step result may be cached
"""

from __future__ import annotations

import functools
from typing import Callable


def pipeline_step(
    name: str = None,
    retries: int = 0,
    cache: bool = False,
) -> Callable:
    """
    Mark a method as a discoverable pipeline step.

    Parameters
    ----------
    name:
        Canonical step name used by the orchestrator (defaults to the
        decorated function's __name__).
    retries:
        How many times the orchestrator should retry on failure.
    cache:
        Whether the orchestrator may cache the step's result.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        wrapper._is_pipeline_step = True
        wrapper._step_name = name or fn.__name__
        wrapper._step_retries = retries
        wrapper._step_cache = cache
        return wrapper

    return decorator
