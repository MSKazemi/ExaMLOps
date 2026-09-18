"""The chart gives each tier a SPIFFE workload identity from the cluster's SPIRE (ADR 0125).

SPIRE's server, node agents, CSI driver and controller manager are cluster infrastructure,
installed once with SPIRE's own hardened chart. With ``workloadIdentity.enabled`` this chart
consumes them:

- a ``ClusterSPIFFEID`` per tier, which the controller manager turns into a registration entry
  for exactly that tier's pods;
- a spiffe-helper sidecar that reaches the Workload API through the SPIFFE CSI driver (no hostPath)
  and keeps the tier's JWT-SVID, or for the control plane the trust bundle, in a memory volume;
- the control plane's verifier settings and a SPIFFE ID → principal map, built from the same
  helper that names the IDs, so the two cannot disagree.

Off by default, and then the chart renders exactly what it did before.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
REGISTRY = ("--set", "global.imageRegistry=ghcr.io/mskazemi/")
ON = ("--set", "workloadIdentity.enabled=true", "--set", "workloadIdentity.trustDomain=example.org")
FOLLOWER = (
    "--set",
    "events.publisher=nats",
    "--set",
    "events.natsUrl=nats://nats.messaging:4222",
    "--set",
    "events.followers.autopilot.enabled=true",
)
NS = "mlops"
SVID = "/run/spire/svid"

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), "--namespace", NS, *REGISTRY, *extra],
        capture_output=True,
        text=True,
    )


def _docs(*extra: str) -> list[dict]:
    out = _helm(*extra)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in docs
        if d["kind"] == "Deployment"
    }


def _pod(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def _container(pod: dict, name: str) -> dict:
    return next(c for c in pod["containers"] if c["name"] == name)


def _env(container: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def _mounts(container: dict) -> dict[str, dict]:
    return {m["name"]: m for m in container.get("volumeMounts", [])}


def _spiffe_id(component: str) -> str:
    return f"spiffe://example.org/ns/{NS}/rel-examlops/{component}"


def test_off_by_default_renders_no_identity_machinery():
    docs = _docs(*FOLLOWER)
    assert not [d for d in docs if d["kind"] == "ClusterSPIFFEID"]
    for deployment in _deployments(docs).values():
        pod = _pod(deployment)
        assert [c["name"] for c in pod["containers"]] == [
            deployment["metadata"]["labels"]["app.kubernetes.io/component"]
        ]
        assert not any("csi" in v for v in pod["volumes"])


def test_enabled_without_a_trust_domain_fails_the_render():
    out = _helm("--set", "workloadIdentity.enabled=true")
    assert out.returncode != 0 and "workloadIdentity.trustDomain" in out.stderr


def test_each_tier_gets_one_cluster_spiffe_id_for_its_own_pods():
    docs = _docs(*ON, *FOLLOWER)
    ids = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ClusterSPIFFEID"}
    components = {"control-plane", "dashboard", "agent", "autopilot-follower"}
    assert set(ids) == {f"{NS}-rel-examlops-{c}" for c in components}
    for component in components:
        spec = ids[f"{NS}-rel-examlops-{component}"]["spec"]
        assert spec["spiffeIDTemplate"] == _spiffe_id(component)
        assert spec["podSelector"]["matchLabels"] == {
            "app.kubernetes.io/name": "examlops",
            "app.kubernetes.io/instance": "rel",
            "app.kubernetes.io/component": component,
        }
        assert spec["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": NS}
        assert spec["jwtTtl"] == "5m" and spec["hint"] == component
        assert "fallback" not in spec  # it must win over SPIRE's default fallback identity


def test_a_disabled_tier_gets_no_identity():
    docs = _docs(*ON, "--set", "agent.enabled=false")
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "ClusterSPIFFEID"}
    assert names == {f"{NS}-rel-examlops-control-plane", f"{NS}-rel-examlops-dashboard"}


def test_cluster_spiffe_ids_can_be_left_to_the_site():
    docs = _docs(*ON, "--set", "workloadIdentity.clusterSPIFFEID.create=false")
    assert not [d for d in docs if d["kind"] == "ClusterSPIFFEID"]
    assert "spiffe-helper" in [
        c["name"] for c in _pod(_deployments(docs)["dashboard"])["containers"]
    ]


def test_callers_read_the_token_their_helper_keeps():
    deployments = _deployments(_docs(*ON, *FOLLOWER))
    for component in ("dashboard", "agent", "autopilot-follower"):
        pod = _pod(deployments[component])
        main, helper = _container(pod, component), _container(pod, "spiffe-helper")
        assert _env(main)["CONTROL_PLANE_TOKEN_FILE"] == f"{SVID}/control-plane.jwt"
        assert _mounts(main)["spiffe-svid"] == {
            "name": "spiffe-svid",
            "mountPath": SVID,
            "readOnly": True,
        }
        assert _mounts(helper)["spiffe-svid"]["mountPath"] == SVID
        assert not _mounts(helper)["spiffe-svid"].get("readOnly")
        assert helper["args"] == ["-config", f"/etc/spiffe-helper/{component}.conf"]


def test_the_helper_is_hardened_like_every_tier_and_uses_the_csi_driver():
    docs = _docs(*ON)
    pod = _pod(_deployments(docs)["dashboard"])
    helper = _container(pod, "spiffe-helper")
    assert helper["securityContext"]["readOnlyRootFilesystem"] is True
    assert helper["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert helper["image"].startswith("ghcr.io/spiffe/spiffe-helper:0.11.0@sha256:")
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["spiffe-workload-api"]["csi"] == {"driver": "csi.spiffe.io", "readOnly": True}
    assert volumes["spiffe-svid"]["emptyDir"]["medium"] == "Memory"
    assert not any("hostPath" in v for v in pod["volumes"])
    assert pod["securityContext"]["runAsNonRoot"] is True


def test_helper_configs_select_each_tiers_own_svid():
    docs = _docs(*ON, *FOLLOWER)
    config = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-spiffe-helper")
    )["data"]
    assert set(config) == {
        f"{c}.conf" for c in ("control-plane", "dashboard", "agent", "autopilot-follower")
    }
    for name, text in config.items():
        assert 'agent_address = "/spiffe-workload-api/spire-agent.sock"' in text
        assert f'hint = "{name.removesuffix(".conf")}"' in text
    assert 'jwt_bundle_file_name = "jwt-bundle.json"' in config["control-plane.conf"]
    assert "jwt_svids" not in config["control-plane.conf"]
    assert 'jwt_audience = "control-plane"' in config["dashboard.conf"]
    for text in config.values():
        for mode in ("jwt_svid_file_mode", "jwt_bundle_file_mode"):
            if mode in text:
                assert f"{mode} = 0644" in text  # a read-only file cannot be renewed


def test_the_control_plane_verifies_and_maps_exactly_the_deployed_callers():
    deployments = _deployments(_docs(*ON, *FOLLOWER))
    pod = _pod(deployments["control-plane"])
    env = _env(_container(pod, "control-plane"))
    assert env["EXAMLOPS_SPIFFE_TRUST_DOMAIN"] == "example.org"
    assert env["EXAMLOPS_SPIFFE_BUNDLE"] == f"{SVID}/jwt-bundle.json"
    assert "CONTROL_PLANE_TOKEN_FILE" not in env
    mapping = json.loads(env["CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON"])
    assert mapping == {
        _spiffe_id("dashboard"): {
            "principal": "dashboard",
            "tenant": "default",
            "scopes": ["read", "write"],
        },
        _spiffe_id("agent"): {
            "principal": "skipper",
            "tenant": "default",
            "scopes": ["read", "retrain"],
        },
        _spiffe_id("autopilot-follower"): {
            "principal": "autopilot",
            "tenant": "default",
            "scopes": ["read", "retrain"],
        },
    }
    without_agent = _deployments(_docs(*ON, "--set", "agent.enabled=false"))
    env = _env(_container(_pod(without_agent["control-plane"]), "control-plane"))
    assert set(json.loads(env["CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON"])) == {
        _spiffe_id("dashboard")
    }


def test_the_values_schema_rejects_unknown_identity_settings():
    out = _helm(*ON, "--set", "workloadIdentity.bogus=1")
    assert out.returncode != 0 and "bogus" in out.stderr
    out = _helm(*ON, "--set", "workloadIdentity.callers.dashboard.scopes={root}")
    assert out.returncode != 0


def test_cluster_spiffe_ids_use_only_fields_spires_crd_declares():
    """The vendored CRD schema (kubeconform validates against it in CI) knows every field used."""
    schema = json.loads(
        (CHART.parent / "schemas" / "spire.spiffe.io" / "clusterspiffeid_v1alpha1.json").read_text()
    )
    declared = schema["properties"]["spec"]["properties"]
    for doc in (d for d in _docs(*ON, *FOLLOWER) if d["kind"] == "ClusterSPIFFEID"):
        assert doc["apiVersion"] == "spire.spiffe.io/v1alpha1"
        assert set(doc["spec"]) <= set(declared), set(doc["spec"]) - set(declared)
        assert set(schema["properties"]["spec"]["required"]) <= set(doc["spec"])


def test_the_default_class_is_the_hardened_charts_documented_install():
    """spire-controller-manager ignores other classes silently; the live kind test found it."""
    docs = _docs(*ON)
    classes = {d["spec"].get("className") for d in docs if d["kind"] == "ClusterSPIFFEID"}
    assert classes == {"spire-mgmt-spire"}
    docs = _docs(*ON, "--set", "workloadIdentity.clusterSPIFFEID.className=")
    assert all("className" not in d["spec"] for d in docs if d["kind"] == "ClusterSPIFFEID")
