"""ADR 0126 decision 2, made true by construction: inference engines are never exposed directly.

An engine (vLLM and its peers) answers with no virtual key, no quota, no guardrail scan and no
audit record, so it may be reached only over the internal network by the gateway / serving plane.
This guard parses every shipped Compose file and renders the Helm chart (when ``helm`` is present)
and fails on:

* an engine Compose service that publishes a host port, in any file except the documented opt-in
  dev overlay ``docker-compose.engine-direct.yml`` (and there only bound to loopback);
* an engine Service of type NodePort/LoadBalancer, or an Ingress routing to an engine, in the chart.

Ray Serve (the predictive serving plane) is a separate concern, guarded by the identity overlay.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit._compose_yaml import load_compose

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
OPT_IN = "docker-compose.engine-direct.yml"
ENGINE = re.compile(r"vllm|sglang|text-generation|tgi\b|triton|kserve|llama[-_.]?cpp|ollama", re.I)
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))


def _is_engine(name: str, svc: dict) -> bool:
    return bool(ENGINE.search(name) or ENGINE.search(str(svc.get("image", ""))))


def _published(entry) -> tuple[str, bool]:
    """(text, is_loopback_only) for one ``ports`` entry, short or long syntax."""
    if isinstance(entry, dict):
        host = str(entry.get("host_ip", ""))
        return str(entry), host in ("127.0.0.1", "::1", "localhost")
    text = str(entry)
    return text, text.startswith(("127.0.0.1:", "[::1]:"))


def _engine_services() -> list[tuple[str, str, dict]]:
    out = []
    for path in sorted(COMPOSE.glob("docker-compose*.yml")):
        doc = load_compose(path) or {}
        for name, svc in (doc.get("services") or {}).items():
            if isinstance(svc, dict) and _is_engine(name, svc):
                out.append((path.name, name, svc))
    return out


def test_the_guard_sees_the_vllm_engine():
    assert any(n == "vllm" for _, n, _ in _engine_services())


def test_no_shipped_compose_file_publishes_an_engine_port_except_the_opt_in_overlay():
    bad = []
    for fname, name, svc in _engine_services():
        ports = svc.get("ports") or []
        if fname == OPT_IN:
            bad += [f"{fname}:{name} {p}" for p in ports if not _published(p)[1]]
        else:
            bad += [f"{fname}:{name} publishes {p!r}" for p in ports]
    assert not bad, "engines must not be published (ADR 0126 decision 2): " + "; ".join(bad)


def test_the_opt_in_overlay_exists_and_actually_publishes_the_engine():
    svc = load_compose(COMPOSE / OPT_IN)["services"]["vllm"]
    assert svc.get("ports"), "the opt-in overlay is the documented way to reach the engine"


def test_the_engine_stays_reachable_on_the_internal_network():
    """The gateway/serving plane reach it by service name on `control` (segmented zones)."""
    seg = load_compose(COMPOSE / "docker-compose.segmented.yml")["services"]["vllm"]
    assert "control" in seg["networks"]
    base = load_compose(COMPOSE / "docker-compose.yml")["services"]["vllm"]
    assert "--port" in str(base["command"]) and "--host 0.0.0.0" in str(base["command"])


def _render(*extra: str) -> list[dict]:
    proc = subprocess.run(
        [
            HELM,
            "template",
            "rel",
            str(CHART),
            "--namespace",
            "mlops",
            "--set",
            "global.imageRegistry=ghcr.io/mskazemi/",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
@pytest.mark.parametrize("extra", [(), ("--set", "gateway.enabled=true")])
def test_helm_renders_no_externally_reachable_engine(extra):
    bad = []
    for doc in _render(*extra):
        kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name", "")
        if kind == "Service" and (doc.get("spec") or {}).get("type") in (
            "NodePort",
            "LoadBalancer",
        ):
            if _is_engine(name, {"image": str(doc)}):
                bad.append(f"Service/{name} {doc['spec']['type']}")
        if kind == "Ingress" and ENGINE.search(yaml.safe_dump(doc.get("spec"))):
            bad.append(f"Ingress/{name} routes to an engine")
    assert not bad, bad


def test_chart_templates_define_no_engine_service_or_ingress_backend():
    """Conservative static fallback (works without helm): no template names an engine at all."""
    hits = [
        p.name
        for p in (CHART / "templates").glob("*.yaml")
        if re.search(r"NodePort|LoadBalancer|kind:\s*Ingress", p.read_text())
        and ENGINE.search(p.read_text())
    ]
    assert not hits, f"templates mixing external exposure with an engine: {hits}"
