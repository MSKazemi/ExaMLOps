"""The bridge's address must be settable, because it is not the same address everywhere.

`DATAPLANE_BUS_BRIDGE_STATUS_URL` is the documented way to say where the Dataplane bus bridge is, and the
dashboard has honoured it since the day the container's own loopback made it report "Bridge
offline". The two `exa` commands that probe the same bridge did not: `exa dataplane-bus status` read a
`dataplane_bus_bridge_url` attribute off `Config` that no `Config` ever had — so the `getattr` fallback
was the only value it could ever produce — and `exa production` wrote `http://localhost:18003`
into its check twice. Neither could be pointed at a bridge on another host, or at the bare-metal
bridge in dev, which serves on `8003` with no port mapping at all. A production-readiness check
that cannot be told where to look reports the wrong verdict with full confidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _hermetic_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("DATAPLANE_BUS_BRIDGE_STATUS_URL", raising=False)


def test_the_default_is_the_host_port_the_stack_publishes():
    from examlops.cli._config import load_config

    assert load_config().dataplane_bus_bridge_url == "http://localhost:18003"


def test_the_documented_env_var_steers_it():
    import os

    from examlops.cli._config import load_config

    os.environ["DATAPLANE_BUS_BRIDGE_STATUS_URL"] = "http://bridge.example:8003"
    try:
        assert load_config().dataplane_bus_bridge_url == "http://bridge.example:8003"
    finally:
        del os.environ["DATAPLANE_BUS_BRIDGE_STATUS_URL"]


def test_production_probes_the_configured_bridge_not_a_hard_coded_one(monkeypatch):
    from examlops.cli._config import Config
    from examlops.cli.commands import production

    asked: list[str] = []

    def fake_get(url, timeout=None):
        asked.append(url)
        return True, {"status": "ok", "inferences_total": 7}, "ok"

    monkeypatch.setattr(production, "_safe_get", fake_get)
    result = production._check_dataplane_bus(
        Config(dataplane_bus_bridge_url="http://elsewhere:9999/")
    )

    assert asked == ["http://elsewhere:9999/health", "http://elsewhere:9999/stats"]
    assert result.ok is True


def test_dataplane_bus_status_reads_the_config_field_that_now_exists():
    """The old code asked `getattr(cfg, ..., default)` for an attribute that was never defined."""
    from examlops.cli._config import Config

    assert hasattr(Config(), "dataplane_bus_bridge_url"), (
        "exa dataplane-bus status resolves the bridge through this field; without it the command "
        "silently falls back and ignores both the config file and the environment"
    )
