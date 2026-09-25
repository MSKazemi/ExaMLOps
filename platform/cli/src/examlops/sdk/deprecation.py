"""The SDK deprecation policy, as code (ADR 0078 clause 3).

Policy (also in ``docs/guides/python-sdk.md``):

1. The public surface is ``examlops.__all__`` plus the ``__all__`` of each SDK namespace
   (``examlops.models``/``drift``/``audit``/``hpc``/``sdk``). Everything else is private.
2. The surface follows SemVer on :func:`examlops.api_version`. Adding a name is a minor bump.
   Removing or incompatibly changing one happens only through point 3 — in a later minor while
   the API is ``0.x`` (SemVer item 4), and only in a later major from ``1.0`` on.
3. A public name is removed only after it has been deprecated for **at least one minor
   release**: it is wrapped with :func:`deprecated`, which records it in :data:`DEPRECATIONS`
   and emits a :class:`DeprecationWarning` naming the replacement on every call.
4. ``tests/unit/test_sdk_deprecation_policy.py`` enforces the window: a deprecation whose
   ``removed_in`` is not a later minor than ``since`` fails the build, and so does one whose
   ``removed_in`` has been reached while the name is still exported.
"""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = ["Deprecation", "DEPRECATIONS", "deprecated", "parse_version", "window_ok"]

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class Deprecation:
    """One deprecated public name and its removal schedule (API versions, ``MAJOR.MINOR``)."""

    name: str
    since: str
    removed_in: str
    replacement: str | None = None

    def message(self) -> str:
        text = (
            f"{self.name} is deprecated since SDK API {self.since} and will be removed in "
            f"{self.removed_in}"
        )
        return f"{text}; use {self.replacement} instead" if self.replacement else text


#: Every deprecation currently in force, keyed by public name.
DEPRECATIONS: dict[str, Deprecation] = {}


def parse_version(value: str) -> tuple[int, int]:
    """``"0.2"`` → ``(0, 2)``. Raises ``ValueError`` on anything else."""
    parts = str(value).strip().split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"SDK API versions are MAJOR.MINOR, got {value!r}")
    return int(parts[0]), int(parts[1])


def window_ok(since: str, removed_in: str) -> bool:
    """True when the warning ships for at least one whole minor line before removal.

    Deprecated in ``0.2`` ⇒ every ``0.2.x`` warns ⇒ the earliest removal is ``0.3``. From ``1.0``
    on a removal is a breaking change, so it must also wait for the next major.
    """
    s, r = parse_version(since), parse_version(removed_in)
    if s[0] >= 1:
        return r[0] > s[0]
    return r > s


def deprecated(
    *, since: str, removed_in: str, replacement: str | None = None, name: str | None = None
) -> Callable[[F], F]:
    """Mark a public callable deprecated; refuses a schedule shorter than the policy allows."""
    if not window_ok(since, removed_in):
        raise ValueError(
            f"deprecation window too short ({since} → {removed_in}): the policy requires the "
            "warning to ship for at least one minor release before removal"
        )

    def wrap(fn: F) -> F:
        dep = Deprecation(
            name=name or f"{fn.__module__}.{fn.__qualname__}",
            since=since,
            removed_in=removed_in,
            replacement=replacement,
        )
        DEPRECATIONS[dep.name] = dep

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(dep.message(), DeprecationWarning, stacklevel=2)
            return fn(*args, **kwargs)

        inner.__deprecated__ = dep  # type: ignore[attr-defined]
        return inner  # type: ignore[return-value]

    return wrap
