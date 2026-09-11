"""ADR 0130 — every registered connector honours the same contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector, Connector  # noqa: E402

# The five built-in connectors — used only for the *payload-specific* cases below (empty-spec
# error text, unreachable-target probe fixtures), whose ``conn``/``spec`` payloads are shaped
# around these specific kinds (e.g. ``bootstrap_servers`` for kafka, ``record`` for zenodo) and
# would not mean anything to an arbitrary plugin.
KINDS = ["sql", "files", "zenodo", "rest", "kafka"]

# Every *registered* connector — built-ins plus any ``exa.dataplane.connectors`` entry-point
# plugin installed in this environment — computed once at collection time the same way the
# registry itself would. The generic contract cases (protocol conformance, ``available()``,
# ``extra`` naming) parametrize over this so a plugin connector is exercised too, not just the
# five shipped with the platform.
registry.reset()
ALL = sorted(c.kind for c in registry.all_connectors())
registry.reset()


@pytest.fixture(autouse=True)
def _fresh():
    registry.reset()
    yield
    registry.reset()


@pytest.mark.parametrize("kind", ALL)
def test_is_a_connector_with_declared_metadata(kind):
    c = registry.get(kind)
    assert isinstance(c, Connector)
    assert c.kind == kind and c.extra and isinstance(c.connection_kinds, tuple)


@pytest.mark.parametrize("kind", KINDS)
def test_empty_spec_is_rejected_with_messages(kind):
    errors = registry.get(kind).validate_spec({})
    assert errors and all(isinstance(e, str) and e for e in errors)


@pytest.mark.parametrize("kind", ALL)
def test_available_never_raises_and_explains(kind):
    ok, why = registry.get(kind).available()
    assert isinstance(ok, bool) and (ok or "examlops[" in why)


def test_a_plugin_connector_is_covered_by_the_generic_parametrization(monkeypatch):
    """An ``exa.dataplane.connectors`` entry-point plugin — not just the five built-ins — must
    show up in ``ALL``, since that's what the generic contract cases above are parametrized over.

    Fakes a discovered entry point (same technique as ``test_dataplane_registry.py``) rather than
    installing a real package, so this stays simple and deterministic.
    """

    class _FakePlugin(BaseConnector):
        kind = "fake-plugin"
        connection_kinds = ("fakeconn",)
        extra = "dataplane-fake-plugin"

    class _FakeEntryPoint:
        name = "fake-plugin"

        def load(self):
            return _FakePlugin

    monkeypatch.setattr(registry, "_entry_points", lambda: [_FakeEntryPoint()])
    registry.reset()
    kinds = [c.kind for c in registry.all_connectors()]
    assert "fake-plugin" in kinds
    # And it passes the same generic assertions the parametrized cases run for every built-in.
    plugin = registry.get("fake-plugin")
    assert isinstance(plugin, Connector)
    ok, why = plugin.available()
    assert isinstance(ok, bool) and (ok or "examlops[" in why)


@pytest.mark.parametrize("kind", KINDS)
def test_probe_on_an_unreachable_target_returns_a_redacted_failure(kind):
    conn = {
        "kind": kind,
        "url": "postgresql+psycopg://u@127.0.0.1:1/x",
        "base_url": "https://127.0.0.1:1",
        "bootstrap_servers": "127.0.0.1:1",
        "secret": "canary-secret-value",
    }
    spec = {
        "table": "t",
        "url": "https://127.0.0.1:1/x.csv",
        "record": 1,
        "path": "/x",
        "topic": "t",
    }
    c = registry.get(kind)
    if not c.available()[0]:
        pytest.skip(f"{kind} extra not installed")
    probe = c.probe(conn, spec)
    assert probe.ok is False and "canary-secret-value" not in probe.detail


@pytest.mark.parametrize("kind", ALL)
def test_extra_follows_the_dataplane_pip_extras_naming_scheme(kind):
    """Every connector's ``extra`` names a real ``examlops[dataplane-*]`` install target."""
    extra = registry.get(kind).extra
    assert extra and extra.startswith("dataplane-")


def test_every_builtin_connector_extra_is_declared_in_pyproject():
    """A connector's ``extra`` must name a real key of ``[project.optional-dependencies]`` —
    otherwise ``pip install 'examlops[<extra>]'`` (the hint ``BaseConnector.available()`` prints
    on a missing dependency) fails outright instead of installing anything.

    Scoped to the five built-ins, not ``ALL``: a third-party plugin (see the fake-plugin case
    above) is free to declare its own extra in its own package, which this repo's pyproject.toml
    has no reason to know about.
    """
    import tomllib
    from pathlib import Path

    manifest = tomllib.loads(
        (Path(__file__).parents[2] / "platform" / "cli" / "pyproject.toml").read_text()
    )
    declared = set(manifest["project"]["optional-dependencies"])
    for kind in KINDS:
        extra = registry.get(kind).extra
        assert extra in declared, f"connector {kind!r} names extra {extra!r}, not in pyproject.toml"
