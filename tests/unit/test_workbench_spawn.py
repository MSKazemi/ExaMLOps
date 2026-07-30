"""Guard for the Platform Ops workbench spawn wiring + shared config-dir override (M3)."""

from __future__ import annotations

import importlib

from examlops.workbench_spawn import PLATFORM_OPS_PROJECT, platform_ops_spawn


def test_normal_project_gets_no_extra_wiring():
    vols, env = platform_ops_spawn("jpcp", is_admin=True, host_repo="/repo")
    assert vols == {} and env == {}


def test_platform_ops_viewer_gets_tier_a_only():
    vols, env = platform_ops_spawn(
        PLATFORM_OPS_PROJECT, is_admin=False, host_repo="/nfs/examlops", actor="alice"
    )
    # Tier A: shared config dir + config env + attribution
    assert "/nfs/examlops/.platform-config" in vols
    assert env["EXAMLOPS_CONFIG_DIR"] == "/home/jovyan/.config/examlops"
    assert env["EXAMLOPS_ADMIN_SOURCE"] == "workbench"
    assert env["EXAMLOPS_ACTOR"] == "alice"
    # Tier B source is NOT mounted for a viewer
    assert not any("platform/clients" in k for k in vols)
    assert "EXAMLOPS_PLATFORM_SOURCE" not in env


def test_platform_ops_admin_gets_tier_b_source():
    vols, env = platform_ops_spawn(
        PLATFORM_OPS_PROJECT, is_admin=True, host_repo="/nfs/examlops", actor="root"
    )
    assert vols["/nfs/examlops/platform/clients"] == {
        "bind": "/repo/platform/clients",
        "mode": "rw",
    }
    assert env["EXAMLOPS_PLATFORM_SOURCE"] == "1"


def test_config_dir_env_override(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", "/shared/cfg")
    import examlops.policy as policy
    import examlops.providers.loader as loader

    importlib.reload(loader)
    importlib.reload(policy)
    assert str(loader.FINOPS_YAML) == "/shared/cfg/finops.yaml"
    assert str(loader.PROVIDERS_YAML) == "/shared/cfg/providers.yaml"
    assert str(policy.POLICY_YAML) == "/shared/cfg/policy.yaml"
    # restore module state for the rest of the suite
    monkeypatch.delenv("EXAMLOPS_CONFIG_DIR", raising=False)
    importlib.reload(loader)
    importlib.reload(policy)
