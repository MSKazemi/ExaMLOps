"""Unit tests for the HPC cluster registry + approval-gated connect (Phase 35b, offline)."""

from __future__ import annotations

import pytest


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """Isolate both sources of truth: clusters.yaml and platform.db."""
    monkeypatch.setenv("EXAMLOPS_HPC_REGISTRY", str(tmp_path / "clusters.yaml"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    from examlops import platform_db

    platform_db.init_db()
    import importlib

    from examlops import hpc_registry

    importlib.reload(hpc_registry)
    return hpc_registry, tmp_path


def _register_flux(reg, name="remote", host="remote-login"):
    reg.register_pending(
        name,
        "flux",
        transport="ssh",
        host=host,
        ssh_user="hpcuser",
        ssh_port=22,
        ssh_key="~/.ssh/id_ed25519",
        capabilities={"scheduler": "flux", "total_gpus": 8},
        requested_by="tester",
    )


def test_register_pending_creates_pending_in_both_sources(registry_env):
    reg, tmp = registry_env
    _register_flux(reg)
    assert (tmp / "clusters.yaml").exists()  # yaml source of truth written

    clusters = reg.list_clusters()
    assert len(clusters) == 1
    c = clusters[0]
    assert c["name"] == "remote"
    assert c["scheduler"] == "flux"
    assert c["state"] == "PENDING"
    assert c["host"] == "remote-login"


def test_pending_cluster_is_not_usable(registry_env):
    reg, _ = registry_env
    _register_flux(reg)
    with pytest.raises(reg.ClusterNotActiveError):
        reg.require_active("remote")


def test_unknown_cluster_raises(registry_env):
    reg, _ = registry_env
    with pytest.raises(reg.ClusterNotActiveError):
        reg.require_active("nope")


def test_approve_flips_to_active_and_resolves_env(registry_env):
    reg, _ = registry_env
    from examlops.platform_db import set_cluster_state

    _register_flux(reg)
    assert set_cluster_state("remote", "ACTIVE", approved_by="admin") is True

    merged = reg.require_active("remote")
    assert merged["state"] == "ACTIVE"

    env = reg.resolve_env("remote")
    assert env["EXAMLOPS_HPC_SCHEDULER"] == "flux"
    assert env["EXAMLOPS_HPC_TRANSPORT"] == "ssh"
    assert env["EXAMLOPS_HPC_SSH_HOST"] == "remote-login"
    assert env["EXAMLOPS_HPC_SSH_USER"] == "hpcuser"
    assert env["EXAMLOPS_HPC_SSH_PORT"] == "22"


def test_reject_blocks_scheduling(registry_env):
    reg, _ = registry_env
    from examlops.platform_db import set_cluster_state

    _register_flux(reg)
    set_cluster_state("remote", "REJECTED", approved_by="admin", reason="wrong account")
    with pytest.raises(reg.ClusterNotActiveError):
        reg.require_active("remote")
    assert reg.get_merged("remote")["reason"] == "wrong account"


def test_reprobe_preserves_approved_state(registry_env):
    """Re-running connect on an ACTIVE cluster must not silently de-authorize it."""
    reg, _ = registry_env
    from examlops.platform_db import set_cluster_state

    _register_flux(reg)
    set_cluster_state("remote", "ACTIVE", approved_by="admin")
    _register_flux(reg)  # re-probe / re-register
    assert reg.get_merged("remote")["state"] == "ACTIVE"


def test_yaml_is_source_of_truth_for_connection(registry_env):
    """A hand-edited clusters.yaml host wins over the DB copy of the definition."""
    reg, tmp = registry_env
    _register_flux(reg, host="old-host")
    # Operator edits clusters.yaml directly.
    import yaml

    path = tmp / "clusters.yaml"
    data = yaml.safe_load(path.read_text())
    data["clusters"]["remote"]["host"] = "new-host"
    path.write_text(yaml.safe_dump(data))

    assert reg.get_merged("remote")["host"] == "new-host"


def test_unmanaged_resolves_to_mock_scheduler(registry_env):
    reg, _ = registry_env
    from examlops.platform_db import set_cluster_state

    reg.register_pending("gpubox", "unmanaged", transport="ssh", host="remote-gpu01")
    set_cluster_state("gpubox", "ACTIVE", approved_by="admin")
    env = reg.resolve_env("gpubox")
    assert env["EXAMLOPS_HPC_SCHEDULER"] == "mock"
    assert env["EXAMLOPS_HPC_SSH_HOST"] == "remote-gpu01"
