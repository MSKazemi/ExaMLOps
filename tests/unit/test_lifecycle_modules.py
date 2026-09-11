"""Site feature profile: catalog, resolution, rendering and CLI enforcement (ADR 0128)."""

from __future__ import annotations

import click
import pytest
import typer
from typer.testing import CliRunner

from examlops.cli.main import _ROOT_PANELS, app
from examlops.lifecycle import modules as mods

runner = CliRunner()


def _root_commands() -> set[str]:
    cmd = typer.main.get_command(app)
    return set(cmd.list_commands(click.Context(cmd)))


def _write(tmp_path, text: str):
    p = tmp_path / "site.toml"
    p.write_text(text)
    return p


# ── the catalog is complete and consistent ────────────────────────────────────────────────


def test_every_root_command_belongs_to_exactly_one_module():
    owned = [c for m in mods.CATALOG for c in m.cli]
    assert len(owned) == len(set(owned)), "a command is claimed by two modules"
    live = _root_commands()
    unplaced = live - set(owned)
    assert not unplaced, (
        f"root commands with no module (add them to examlops.lifecycle.modules.CATALOG): "
        f"{sorted(unplaced)}"
    )
    stale = set(owned) - live
    assert not stale, f"CATALOG names commands the CLI does not have: {sorted(stale)}"


def test_catalog_references_are_real():
    ids = set(mods.module_ids())
    assert len(ids) == len(mods.CATALOG)
    for m in mods.CATALOG:
        assert set(m.requires) <= ids, m.id
    for name, (_desc, members) in mods.PRESETS.items():
        assert set(members) <= ids, name
        assert "core" in members, f"preset {name} must include core"
    assert set(mods.PRESETS["full"][1]) == ids
    assert [m.id for m in mods.CATALOG if m.required] == ["core"]


def test_panel_spec_and_catalog_agree_on_the_command_set():
    paneled = {n for _title, names in _ROOT_PANELS for n in names}
    owned = {c for m in mods.CATALOG for c in m.cli}
    assert paneled == owned


def test_compose_services_and_profiles_exist_in_the_compose_file():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "platform/infra/docker-compose/docker-compose.yml"
        ).read_text()
    )
    services = compose["services"]
    profiles = {p for svc in services.values() for p in svc.get("profiles", [])}
    for m in mods.CATALOG:
        for svc in m.compose_services:
            assert svc in services, f"{m.id}: no compose service {svc!r}"
            assert not services[svc].get("profiles"), f"{svc} is already profile-gated"
        for prof in m.compose_profiles:
            assert prof in profiles, f"{m.id}: no compose profile {prof!r}"


def test_dashboard_pages_exist_in_the_frontend_navigation():
    """A module's dashboard pages must be real nav routes, or hiding them would hide nothing."""
    import re
    from pathlib import Path

    nav = (
        Path(__file__).resolve().parents[2] / "platform/services/dashboard/frontend/src/lib/nav.ts"
    ).read_text()
    routes = set(re.findall(r"path: '([^']+)'", nav))
    owned = [p for m in mods.CATALOG for p in m.dashboard_pages]
    assert len(owned) == len(set(owned)), "a dashboard page is claimed by two modules"
    missing = sorted(set(owned) - routes)
    assert not missing, f"CATALOG dashboard_pages not in the frontend nav: {missing}"
    assert all(m.dashboard_pages == () for m in mods.CATALOG if m.required)  # core is never hidden


def test_mcp_tags_name_real_tools_and_tools_follow_the_profile(monkeypatch):
    """Agents see the same site as the CLI: a disabled module's MCP tools are not offered."""
    from examlops.mcp.tools import REGISTRY, iter_tools

    used = {t for spec in REGISTRY for t in spec.tags}
    mapped = [t for m in mods.CATALOG for t in m.mcp_tags]
    assert len(mapped) == len(set(mapped)), "an MCP tag is claimed by two modules"
    assert not set(mapped) - used, f"mcp_tags no tool carries: {sorted(set(mapped) - used)}"

    everything = {s.name for s in iter_tools(include_writes=True)}
    assert everything == {s.name for s in REGISTRY}  # default profile: nothing filtered
    monkeypatch.setenv(mods.FEATURES_ENV, "-hpc,-finops")
    offered = {s.name for s in iter_tools(include_writes=True)}
    assert {"hpc_clusters", "hpc_nodes", "fleet_simulate", "carbon", "model_costs"} <= (
        everything - offered
    )
    assert {"platform_status", "list_models", "list_approvals", "drift_status"} <= offered


# ── resolution ────────────────────────────────────────────────────────────────────────────


def test_no_profile_means_everything_on():
    profile = mods.resolve(site=mods.SiteFile("x", False), env={})
    assert profile.preset == "full" and not profile.disabled_ids()
    assert profile.spec() == "preset:full"


def test_env_overlays_the_site_file(tmp_path):
    sf = mods.read_site_file(
        _write(tmp_path, '[features]\npreset = "standard"\nenable = ["hpc"]\n')
    )
    p = mods.resolve(site=sf, env={})
    assert p.preset == "standard" and p.is_enabled("hpc") and not p.is_enabled("genai")
    p2 = mods.resolve(site=sf, env={mods.FEATURES_ENV: "-hpc,+genai"})
    assert not p2.is_enabled("hpc") and p2.is_enabled("genai")
    assert p2.reasons["genai"] == f"enabled by {mods.FEATURES_ENV}"
    p3 = mods.resolve(site=sf, env={mods.FEATURES_ENV: "preset:minimal"})
    assert p3.preset == "minimal" and p3.is_enabled("hpc")  # the file's enable still layers on


def test_core_cannot_be_disabled():
    p = mods.resolve(site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "-core"})
    assert p.is_enabled("core") and any("required" in w for w in p.warnings)
    with pytest.raises(ValueError, match="required"):
        mods.set_modules(disable=["core"])


def test_enabling_pulls_dependencies_in():
    p = mods.resolve(
        site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "preset:minimal,+autopilot"}
    )
    assert p.is_enabled("autopilot") and p.is_enabled("quality")
    assert p.reasons["quality"] == "required by 'autopilot'"


def test_explicitly_disabled_dependency_switches_the_dependent_off():
    p = mods.resolve(site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "-genai"})
    assert not p.is_enabled("genai") and not p.is_enabled("llm-serving")
    assert "requires 'genai'" in p.reasons["llm-serving"]


def test_unknown_names_are_warnings_not_crashes(tmp_path):
    sf = mods.read_site_file(_write(tmp_path, '[features]\npreset = "nope"\nenable = ["warp"]\n'))
    p = mods.resolve(site=sf, env={mods.FEATURES_ENV: "+ghost"})
    assert p.preset == "full"
    assert len(p.warnings) == 3


def test_unreadable_site_file_is_reported(tmp_path):
    sf = mods.read_site_file(_write(tmp_path, "[features\n"))
    assert sf.error
    assert mods.resolve(site=sf, env={}).warnings


def test_spec_round_trips_through_the_env():
    p = mods.resolve(
        site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "preset:standard,+hpc,-finops"}
    )
    again = mods.resolve(site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: p.spec()})
    assert again.enabled == p.enabled


# ── the site file ─────────────────────────────────────────────────────────────────────────


def test_site_file_writes_are_validated_and_atomic(tmp_path, monkeypatch):
    path = tmp_path / "site.toml"
    monkeypatch.setenv(mods.SITE_PROFILE_ENV, str(path))
    mods.set_preset("standard", site_name="centre-a")
    mods.set_modules(enable=["hpc"])
    mods.set_modules(disable=["hpc"])  # the later decision wins, no duplicates
    sf = mods.read_site_file(path)
    assert (sf.preset, sf.name, sf.enable, sf.disable) == ("standard", "centre-a", [], ["hpc"])
    with pytest.raises(KeyError):
        mods.set_modules(enable=["warp"])
    assert mods.reset_site_file() == str(path) and not path.exists()


def test_site_profile_lives_in_the_data_root(tmp_path, monkeypatch):
    monkeypatch.delenv(mods.SITE_PROFILE_ENV)
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert mods.site_profile_path() == (tmp_path / "site.toml", "data-root")


# ── surfaces ──────────────────────────────────────────────────────────────────────────────


def test_api_paths_and_flags_map_to_modules():
    assert mods.module_for_api_path("/api/v1/facility/fleet") == "hpc"
    assert mods.module_for_api_path("/api/v1/projects") is None
    assert mods.module_for_flag("llmopsConsole") == "genai"
    assert mods.module_for_command("hpc") == "hpc"
    assert mods.module_for_command("some-plugin") is None


def test_render_compose_parks_disabled_services_and_relaxes_their_dependents():
    p = mods.resolve(site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "preset:standard"})
    out = mods.render_compose(p)
    assert out["env"]["COMPOSE_PROFILES"] == "jupyter,monitoring"
    svc = out["override"]["services"]
    assert svc["agent"] == {"profiles": [mods.COMPOSE_DISABLED_PROFILE]}
    assert svc["dashboard"]["depends_on"]["agent"]["required"] is False
    full = mods.render_compose(mods.resolve(site=mods.SiteFile("x", False), env={}))
    assert full["override"] is None


def test_render_helm_switches_the_agent_tier():
    p = mods.resolve(site=mods.SiteFile("x", False), env={mods.FEATURES_ENV: "-agent"})
    assert mods.render_helm(p) == {
        "site": {"features": "preset:full,-agent"},
        "agent": {"enabled": False},
    }


def test_disabled_command_is_hidden_and_refused(monkeypatch):
    monkeypatch.setenv(mods.FEATURES_ENV, "-hpc")
    assert "hpc" not in _root_commands()
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0 and " hpc " not in res.output
    res = runner.invoke(app, ["hpc", "clusters"])
    assert res.exit_code == 3
    text = " ".join(res.output.split())  # Rich wraps at the terminal width
    assert "disabled at this site" in text and "exa modules enable hpc" in text
    res = runner.invoke(app, ["hpc", "--help"])  # even help says why, not "no such command"
    assert res.exit_code == 3


def test_core_commands_are_never_gated(monkeypatch):
    monkeypatch.setenv(mods.FEATURES_ENV, "preset:minimal")
    live = _root_commands()
    assert {"modules", "instance", "upgrade", "status", "backup"} <= live
    assert "hpc" not in live and "rag" not in live
