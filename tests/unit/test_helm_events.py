"""The chart's event backbone and its followers (ADR 0124, plan P2).

``events.publisher`` sets the control plane's outbox relay for every tier through the shared
ConfigMap, and ``events.followers`` deploys the two durable consumers Compose runs under the
``events`` profile. Each follower keeps the chart's guarantees: the same pod and container
hardening as every tier, one replica with no overlap on rollout, its own NetworkPolicy tier with
no ingress, and a render that refuses a follower nothing could ever reach.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))
REGISTRY = ("--set", "global.imageRegistry=ghcr.io/mskazemi/")
NATS = ("--set", "events.publisher=nats", "--set", "events.natsUrl=nats://nats.messaging:4222")
FOLLOWERS = (
    "--set",
    "events.followers.autopilot.enabled=true",
    "--set",
    "events.followers.skipperWatch.enabled=true",
)

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), *REGISTRY, *extra], capture_output=True, text=True
    )


def _docs(*extra: str) -> list[dict]:
    out = _helm(*extra)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _by(kind: str, docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"].get("app.kubernetes.io/component", d["metadata"]["name"]): d
        for d in docs
        if d["kind"] == kind
    }


def _config(docs: list[dict]) -> dict[str, str]:
    return next(d for d in docs if d["kind"] == "ConfigMap")["data"]


def _ports(rules: list[dict]) -> set[int]:
    return {p["port"] for r in rules for p in r.get("ports", [])}


def test_the_default_is_the_log_publisher_and_no_follower():
    docs = _docs()
    assert _config(docs)["EXAMLOPS_EVENT_PUBLISHER"] == "log"
    assert "EXAMLOPS_NATS_URL" not in _config(docs)
    assert not {"autopilot-follower", "skipper-watch"} & set(_by("Deployment", docs))


def test_the_control_plane_takes_the_publisher_from_the_shared_config():
    """A container `env` entry overrides `envFrom`: left in controlPlane.env, the old default would
    silently keep the control plane on `log` whatever `events.publisher` said."""
    docs = _docs(*NATS)
    assert _config(docs)["EXAMLOPS_EVENT_PUBLISHER"] == "nats"
    assert _config(docs)["EXAMLOPS_NATS_URL"] == "nats://nats.messaging:4222"
    container = _by("Deployment", docs)["control-plane"]["spec"]["template"]["spec"]["containers"][
        0
    ]
    assert "EXAMLOPS_EVENT_PUBLISHER" not in {e["name"] for e in container.get("env", [])}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--set", "events.publisher=nats"), "events.natsUrl is empty"),
        (("--set", "events.followers.autopilot.enabled=true"), "publisher is not nats"),
    ],
)
def test_a_follower_nothing_can_reach_is_refused(extra, message):
    out = _helm(*extra)
    assert out.returncode != 0 and message in out.stderr


def test_followers_keep_every_tiers_hardening():
    docs = _docs(*NATS, *FOLLOWERS)
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    deployments = _by("Deployment", docs)
    for name, command, image in (
        ("autopilot-follower", ["exa", "autopilot", "follow"], "examlops-control-plane"),
        ("skipper-watch", ["python", "-m", "skipper.watch", "--daemon"], "examlops-agent"),
    ):
        spec = deployments[name]["spec"]
        pod = spec["template"]["spec"]
        container = pod["containers"][0]
        assert spec["replicas"] == 1
        assert spec["strategy"]["rollingUpdate"] == {"maxUnavailable": 1, "maxSurge": 0}
        assert pod["securityContext"] == values["podSecurityContext"]
        assert container["securityContext"] == values["containerSecurityContext"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert pod["securityContext"]["runAsUser"] == 10001
        assert container["command"] == command
        assert f"/{image}:" in container["image"]
        env = {e["name"]: e for e in container["env"]}
        assert env["HOME"]["value"] == "/tmp"  # writable, the root filesystem is not
    services = _by("Service", docs)
    assert not {"autopilot-follower", "skipper-watch"} & set(services)


def test_the_autopilot_follower_uses_its_own_credential_when_there_is_one():
    docs = _docs(*NATS, *FOLLOWERS)
    container = _by("Deployment", docs)["autopilot-follower"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    ref = {e["name"]: e for e in container["env"]}["CONTROL_PLANE_TOKEN"]["valueFrom"]
    assert ref["secretKeyRef"]["key"] == "AUTOPILOT_CONTROL_PLANE_TOKEN"
    assert ref["secretKeyRef"]["optional"] is True  # absent: the shared token from envFrom stays


def test_each_follower_is_its_own_tier_with_no_ingress():
    policies = _by("NetworkPolicy", _docs("--set", "networkPolicy.enabled=true", *NATS, *FOLLOWERS))
    for tier in ("autopilot-follower", "skipper-watch"):
        spec = policies[tier]["spec"]
        assert spec["policyTypes"] == ["Ingress", "Egress"]
        assert not spec.get("ingress")  # nothing calls a follower
        assert 4222 in _ports(spec["egress"])
    peers = [
        p["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        for r in policies["autopilot-follower"]["spec"]["egress"]
        for p in r.get("to", [])
        if "podSelector" in p
    ]
    assert peers == ["control-plane"]
    watch_peers = [p for r in policies["skipper-watch"]["spec"]["egress"] for p in r.get("to", [])]
    assert not watch_peers  # NATS and the datastore only, by port
    cp_ingress = [
        p["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        for r in policies["control-plane"]["spec"]["ingress"]
        for p in r.get("from", [])
        if "podSelector" in p
    ]
    assert "autopilot-follower" in cp_ingress and "skipper-watch" not in cp_ingress


def test_nats_egress_is_opened_only_with_the_nats_publisher():
    off = _by("NetworkPolicy", _docs("--set", "networkPolicy.enabled=true"))
    on = _by("NetworkPolicy", _docs("--set", "networkPolicy.enabled=true", *NATS))
    for tier in ("control-plane", "dashboard"):
        assert 4222 not in _ports(off[tier]["spec"]["egress"])
        assert 4222 in _ports(on[tier]["spec"]["egress"])


@pytest.mark.parametrize(
    "extra",
    [
        ("--set", "events.publisher=kafka"),
        ("--set", "events.natsUrl=nats.messaging:4222"),
        ("--set", "events.followers.autopilot.replicas=2"),
        ("--set", "events.surprise=true"),
    ],
)
def test_the_schema_rejects_what_the_chart_would_ignore(extra):
    assert _helm(*extra).returncode != 0
