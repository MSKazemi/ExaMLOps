"""The Compose identity overlay stays consistent (ADR 0125).

`docker-compose.identity.yml` gives each service that calls the control plane a SPIFFE identity
through `identity/compose.yml`. Four facts must agree, and nothing but reading the files checks
them before a live run: the service reads the volume its helper writes, the helper's label is a
registered workload, the control plane maps that SPIFFE ID to the principal and scopes the service
already had, and the helper runs under the same profile as its service. The live proof is
`tests/integration/test_spire_compose_attestation_live.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit._compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
IDENTITY = COMPOSE / "identity"
DOMAIN = "examlops.internal"
SVID_DIR = "/run/spire/svid"


def _load(path: Path) -> dict:
    return load_compose(path)


@pytest.fixture(scope="module")
def files():
    return {
        "base": _load(COMPOSE / "docker-compose.yml"),
        "overlay": _load(COMPOSE / "docker-compose.identity.yml"),
        "spire": _load(IDENTITY / "compose.yml"),
    }


def _default(value: str) -> str:
    """A Compose ``${VAR:-default}`` → its default."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", value)


def _volume_at(service: dict, target: str) -> tuple[str, bool] | None:
    for entry in service.get("volumes", []):
        parts = entry.split(":")
        if len(parts) >= 2 and parts[1] == target:
            return parts[0], parts[2:] == ["ro"]
    return None


def _helpers(files) -> dict[str, dict]:
    return {
        name: svc
        for name, svc in files["spire"]["services"].items()
        if name.startswith("spiffe-helper-")
    }


def _workload_map(files) -> dict:
    raw = files["overlay"]["services"]["control-plane"]["environment"][
        "CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON"
    ]
    return json.loads(_default(raw))


def test_the_overlay_includes_the_spire_file(files):
    assert files["overlay"]["include"] == ["identity/compose.yml"]


def test_every_caller_reads_the_volume_its_labelled_helper_writes(files):
    by_volume = {}
    for name, helper in _helpers(files).items():
        volume, read_only = _volume_at(helper, SVID_DIR)
        assert not read_only, name
        by_volume[volume] = helper["labels"]["examlops.spiffe"]
    callers = {
        name: svc
        for name, svc in files["overlay"]["services"].items()
        if "CONTROL_PLANE_TOKEN_FILE" in svc.get("environment", {})
    }
    assert set(callers) == {"dashboard", "agent", "autopilot-follower", "seanerbus-bridge"}
    mapping = _workload_map(files)
    for name, svc in callers.items():
        assert svc["environment"]["CONTROL_PLANE_TOKEN_FILE"] == f"{SVID_DIR}/control-plane.jwt"
        volume, read_only = _volume_at(svc, SVID_DIR)
        assert read_only, f"{name} must not be able to write its own credential"
        label = by_volume[volume]
        assert f"spiffe://{DOMAIN}/{label}" in mapping, f"{name}'s identity is not mapped"


def test_the_mapping_keeps_each_services_principal_and_scopes(files):
    """The same principal and scopes as the static credential (plan P3.2), so the audit is continuous."""
    mapping = _workload_map(files)
    assert mapping == {
        f"spiffe://{DOMAIN}/seanerbus-bridge": {
            "principal": "seanerbus-bridge",
            "tenant": "default",
            "scopes": ["retrain"],
        },
        f"spiffe://{DOMAIN}/autopilot": {
            "principal": "autopilot",
            "tenant": "default",
            "scopes": ["read", "retrain"],
        },
        f"spiffe://{DOMAIN}/skipper": {
            "principal": "skipper",
            "tenant": "default",
            "scopes": ["read", "retrain"],
        },
        f"spiffe://{DOMAIN}/dashboard": {
            "principal": "dashboard",
            "tenant": "default",
            "scopes": ["read", "write"],
        },
    }


def test_the_control_plane_verifies_against_its_helpers_bundle(files):
    cp = files["overlay"]["services"]["control-plane"]
    helper = _helpers(files)["spiffe-helper-control-plane"]
    assert cp["environment"]["EXAMLOPS_SPIFFE_BUNDLE"] == f"{SVID_DIR}/jwt-bundle.json"
    assert _volume_at(cp, SVID_DIR) == (_volume_at(helper, SVID_DIR)[0], True)
    assert "./helper-verifier.conf:/conf/helper.conf:ro" in helper["volumes"]


def test_every_identity_label_is_a_registered_workload(files):
    """Helpers carry the callers' labels; the gateway's two Envoys (mutual TLS) carry their own."""
    registered = set(
        _default(
            files["spire"]["services"]["spire-register"]["environment"]["SPIFFE_WORKLOADS"]
        ).split()
    )
    labels = {h["labels"]["examlops.spiffe"] for h in _helpers(files).values()}
    labels |= {
        svc["labels"]["examlops.spiffe"]
        for svc in files["overlay"]["services"].values()
        if "examlops.spiffe" in (svc.get("labels") or {})
    }
    assert labels == registered


def test_a_helper_runs_under_its_services_profile(files):
    base = files["base"]["services"]
    owner = {
        "spiffe-helper-dashboard": "dashboard",
        "spiffe-helper-skipper": "agent",
        "spiffe-helper-autopilot": "autopilot-follower",
        "spiffe-helper-seanerbus-bridge": "seanerbus-bridge",
        "spiffe-helper-control-plane": "control-plane",
    }
    assert set(owner) == set(_helpers(files))
    for helper, service in owner.items():
        assert _helpers(files)[helper].get("profiles") == base[service].get("profiles"), helper


def test_helper_files_stay_writable_by_the_helper():
    """Without capabilities root cannot rewrite a read-only file: renewal failed at 0444."""
    for conf in ("helper-caller.conf", "helper-verifier.conf"):
        modes = re.findall(r"^jwt_\w+_file_mode = (0\d+)$", (IDENTITY / conf).read_text(), re.M)
        assert modes, conf
        for mode in modes:
            assert int(mode, 8) & 0o200 and int(mode, 8) & 0o004, (conf, mode)


def test_spire_is_isolated_and_pinned(files):
    spire = files["spire"]
    assert all(net.get("internal") for net in spire["networks"].values())
    services = spire["services"]
    assert services["spire-docker-proxy"]["networks"] == ["spire_attest"]
    assert services["spire-docker-proxy"]["environment"]["POST"] == "0"
    assert ":ro" in services["spire-docker-proxy"]["volumes"][0]
    assert set(services["spire-agent"]["networks"]) == {"spire", "spire_attest"}
    assert services["spire-server"]["networks"] == ["spire"]
    for name, svc in services.items():
        assert "ports" not in svc, f"{name} publishes a port"
        image = svc.get("image") or ""
        if "spire-init" not in image:
            assert "@sha256:" in image, f"{name} is not pinned by digest"


def test_the_agent_never_bootstraps_insecurely():
    agent = (IDENTITY / "agent.conf").read_text()
    assert "insecure_bootstrap" not in agent
    assert 'trust_bundle_path = "/pki/server-bundle.pem"' in agent
    assert (
        'NodeAttestor "x509pop"' in agent
        and 'NodeAttestor "x509pop"' in (IDENTITY / "server.conf").read_text()
    )
