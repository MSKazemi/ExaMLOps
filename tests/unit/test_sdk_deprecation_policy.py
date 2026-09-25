"""ADR 0078 clause 3 — the SDK deprecation policy, enforced.

A public name leaves the SDK only after its ``DeprecationWarning`` has shipped for at least one
minor release (and, from API 1.0, only in a later major). These tests pin the mechanism and fail
the build when a scheduled removal is due but the name is still exported.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

import examlops
from examlops.sdk import deprecation
from examlops.sdk.deprecation import DEPRECATIONS, deprecated, parse_version, window_ok


@pytest.mark.parametrize(
    ("since", "removed_in", "ok"),
    [
        ("0.2", "0.3", True),  # the whole 0.2 line warns
        ("0.2", "0.2", False),  # removed in the release that deprecated it
        ("0.3", "0.2", False),
        ("0.9", "1.0", True),
        ("1.2", "1.5", False),  # from 1.0 a removal is breaking: next major only
        ("1.2", "2.0", True),
    ],
)
def test_the_window(since, removed_in, ok):
    assert window_ok(since, removed_in) is ok


def test_versions_are_major_minor():
    assert parse_version("0.12") == (0, 12)
    with pytest.raises(ValueError):
        parse_version("0.1.2")


def test_the_decorator_refuses_a_short_window():
    with pytest.raises(ValueError, match="window too short"):
        deprecated(since="0.2", removed_in="0.2")


def test_a_deprecated_callable_warns_names_the_replacement_and_still_works(monkeypatch):
    monkeypatch.setattr(deprecation, "DEPRECATIONS", {})

    @deprecated(since="0.2", removed_in="0.3", replacement="examlops.new_thing", name="old")
    def old(x: int) -> int:
        return x + 1

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert old(1) == 2
    [w] = caught
    assert issubclass(w.category, DeprecationWarning)
    assert "examlops.new_thing" in str(w.message) and "0.3" in str(w.message)
    assert deprecation.DEPRECATIONS["old"].removed_in == "0.3"
    assert old.__deprecated__.since == "0.2"


def _exported(dotted: str) -> bool:
    module_name, _, attr = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    return attr in getattr(module, "__all__", [])


def test_no_deprecated_name_outlives_its_removal_version():
    """The build's side of the policy: a due removal cannot be forgotten."""
    current = parse_version(examlops.api_version())
    overdue = [
        d.name
        for d in DEPRECATIONS.values()
        if parse_version(d.removed_in) <= current and _exported(d.name)
    ]
    assert not overdue, f"deprecations past their removal version still exported: {overdue}"


def test_every_registered_deprecation_honours_the_window():
    bad = [d.name for d in DEPRECATIONS.values() if not window_ok(d.since, d.removed_in)]
    assert not bad, bad
