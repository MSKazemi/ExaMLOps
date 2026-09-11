"""ADR 0130 — connector registry: built-ins, plugins, availability, connection kinds."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane import types as t  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_registry():
    registry.reset()
    yield
    registry.reset()


class _Fake(BaseConnector):
    kind = "fake"
    connection_kinds = ("fakeconn",)
    required_spec = ("thing",)
    connection_required = False


def test_builtins_are_listed_even_without_their_optional_deps():
    kinds = {c.kind for c in registry.all_connectors()}
    assert {"sql", "files", "zenodo", "rest", "kafka"} <= kinds


def test_unknown_kind_names_the_known_ones():
    with pytest.raises(t.SpecError, match="known: .*sql"):
        registry.get("nope")


def test_register_and_connection_kinds():
    registry.register(_Fake())
    assert registry.get("fake").kind == "fake"
    assert "fakeconn" in registry.connection_kinds()
    with pytest.raises(t.SpecError, match="already registered"):
        registry.register(_Fake())


def test_base_validate_spec_reports_missing_keys():
    assert _Fake().validate_spec({}) == ["spec.thing is required"]
    assert _Fake().validate_spec({"thing": 1}) == []


def test_available_reports_the_install_hint():
    class _NeedsGhost(BaseConnector):
        kind = "ghost"
        extra = "dataplane-ghost"
        requires = ("definitely_not_a_module_xyz",)

    ok, why = _NeedsGhost().available()
    assert ok is False
    assert "examlops[dataplane-ghost]" in why


def test_broken_plugin_is_recorded_not_raised(monkeypatch):
    class _EP:
        name = "broken"

        def load(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(registry, "_entry_points", lambda: [_EP()])
    registry.reset()
    registry.all_connectors()
    assert "broken" in registry.plugin_errors()


def test_limits_capped_takes_the_smaller_bound():
    a = t.Limits(max_rows=10, max_bytes=None, max_seconds=5.0)
    b = t.Limits(max_rows=100, max_bytes=50, max_seconds=None)
    assert a.capped(b) == t.Limits(max_rows=10, max_bytes=50, max_seconds=5.0)


def test_global_limits_read_env(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_MAX_ROWS", "7")
    assert t.global_limits().max_rows == 7
