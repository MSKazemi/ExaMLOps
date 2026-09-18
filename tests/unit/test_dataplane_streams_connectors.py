"""Stream connector registry (ADR 0131, Plan 2, task A7): built-ins, register/get, plugins.

Plugins are exercised the way a real install finds them: ``importlib.metadata.entry_points`` is
monkeypatched to return real ``EntryPoint`` objects whose target module sits in ``sys.modules``
— nothing touches ``sys.path``.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import sys
import types
from typing import Any

import pytest

from examlops.dataplane.streams import connectors
from examlops.dataplane.streams.kafka_stream import KafkaStreamConnector
from examlops.dataplane.types import SpecError


@pytest.fixture(autouse=True)
def _fresh_registry():
    connectors.reset()
    yield
    connectors.reset()


class _Fake:
    kind = "fakestream"
    singleton = True
    connection_kinds = ("fakeconn",)

    def run(self, binding: Any, ingress: Any, stop_event: Any, status_cb: Any) -> None:
        status_cb("stopped", None)


def _entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[importlib.metadata.EntryPoint]):
    real = importlib.metadata.entry_points

    def fake(**kwargs: Any) -> Any:
        if kwargs.get("group") == connectors.ENTRY_POINT_GROUP:
            return eps
        return real(**kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)


def _plugin_module(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> str:
    name = "exa_test_stream_plugin_mod"
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return name


def _ep(name: str, value: str) -> importlib.metadata.EntryPoint:
    return importlib.metadata.EntryPoint(name, value, connectors.ENTRY_POINT_GROUP)


def test_kafka_is_a_builtin_and_not_a_singleton():
    kafka = connectors.get("kafka")
    assert isinstance(kafka, KafkaStreamConnector)
    assert kafka.singleton is False and kafka.connection_kinds == ("kafka",)
    assert isinstance(kafka, connectors.StreamConnector)
    assert "kafka" in connectors.connection_kinds()


def test_unknown_kind_is_a_spec_error_naming_the_known_ones():
    with pytest.raises(SpecError, match="known: .*kafka"):
        connectors.get("nope")


def test_register_get_and_duplicate_refused():
    connectors.register(_Fake())
    assert connectors.get("fakestream").kind == "fakestream"
    assert "fakeconn" in connectors.connection_kinds()
    with pytest.raises(SpecError, match="already registered"):
        connectors.register(_Fake())
    with pytest.raises(SpecError, match="already registered"):
        connectors.register(KafkaStreamConnector())  # a built-in kind is a duplicate too


def test_replace_true_swaps_the_connector():
    first, second = _Fake(), _Fake()
    connectors.register(first)
    connectors.register(second, replace=True)
    assert connectors.get("fakestream") is second


def test_register_refuses_something_that_is_not_a_connector():
    class _NoRun:
        kind = "norun"
        singleton = False
        connection_kinds = ()

    with pytest.raises(SpecError, match="run must be callable"):
        connectors.register(_NoRun())  # type: ignore[arg-type]
    with pytest.raises(SpecError, match="singleton"):
        bad = _Fake()
        bad.singleton = "yes"  # type: ignore[assignment]
        connectors.register(bad)


def test_an_entry_point_plugin_is_loaded(monkeypatch: pytest.MonkeyPatch):
    module = _plugin_module(monkeypatch, FakeStream=_Fake)
    _entry_points(monkeypatch, [_ep("fake", f"{module}:FakeStream")])
    connectors.reset()
    loaded = connectors.get("fakestream")
    assert isinstance(loaded, _Fake)  # the class was instantiated
    assert connectors.plugin_errors() == {}
    assert [c.kind for c in connectors.all_connectors()] == ["fakestream", "kafka"]


def test_a_plugin_instance_is_used_as_is(monkeypatch: pytest.MonkeyPatch):
    instance = _Fake()
    module = _plugin_module(monkeypatch, INSTANCE=instance)
    _entry_points(monkeypatch, [_ep("inst", f"{module}:INSTANCE")])
    connectors.reset()
    assert connectors.get("fakestream") is instance


def test_broken_misshapen_and_duplicate_plugins_are_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Shadow(_Fake):
        kind = "kafka"  # tries to replace the built-in

    class _Shapeless:
        kind = "shapeless"

    module = _plugin_module(monkeypatch, Shadow=_Shadow, Shapeless=_Shapeless)
    _entry_points(
        monkeypatch,
        [
            _ep("broken", f"{module}:DoesNotExist"),
            _ep("shadow", f"{module}:Shadow"),
            _ep("shapeless", f"{module}:Shapeless"),
        ],
    )
    connectors.reset()
    assert isinstance(connectors.get("kafka"), KafkaStreamConnector)  # the built-in wins
    errors = connectors.plugin_errors()
    assert set(errors) == {"broken", "shadow", "shapeless"}
    assert "AttributeError" in errors["broken"]
    assert "already registered" in errors["shadow"]
    assert "not a stream connector" in errors["shapeless"]
    with pytest.raises(SpecError):
        connectors.get("shapeless")


def test_the_registry_never_touches_sys_path():
    source = inspect.getsource(connectors)
    assert "sys.path" not in source.replace("``sys.path``", "")
    assert "import sys" not in source


def test_a_broken_builtin_is_recorded_and_the_rest_still_load(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(connectors._BUILTINS, "ghost", "examlops.no_such_module_xyz:Ghost")
    module = _plugin_module(monkeypatch, FakeStream=_Fake)
    _entry_points(monkeypatch, [_ep("fake", f"{module}:FakeStream")])
    connectors.reset()
    assert isinstance(connectors.get("kafka"), KafkaStreamConnector)
    assert connectors.get("fakestream").kind == "fakestream"  # plugins still load
    assert "ModuleNotFoundError" in connectors.plugin_errors()["builtin:ghost"]


def test_plugin_error_text_is_redacted(monkeypatch: pytest.MonkeyPatch):
    class _Leaky:
        def __init__(self) -> None:
            raise RuntimeError("cannot reach db: password=hunter2")

    module = _plugin_module(monkeypatch, Leaky=_Leaky)
    _entry_points(monkeypatch, [_ep("leaky", f"{module}:Leaky")])
    connectors.reset()
    error = connectors.plugin_errors()["leaky"]
    assert "RuntimeError" in error and "hunter2" not in error
