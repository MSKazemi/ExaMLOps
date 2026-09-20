"""Opt-in services are scraped when they run, and absent ones raise no alert (Docker + Prometheus).

``prometheus.yml`` finds the services behind Compose profiles (``gateway``, ``gateway-authz``,
``vllm``, ``dataplane-bus-bridge``) by DNS instead of listing them as static targets: a static target
for a service the site does not run is an ``up == 0`` series forever, so ``TargetDown`` and the
service's own alert fire permanently. Whether that works depends on Prometheus's DNS discovery
against Docker's embedded resolver, which no unit test can show. This runs the Prometheus image
the platform ships, with the committed opt-in scrape jobs, on a private Docker network where only
the gateway runs (the real Envoy and ``envoy.yaml``):

- the gateway is discovered and scraped (``up{job="gateway"} == 1``);
- the absent services produce no ``up`` series at all, so no alert selects them;
- a gateway that stops is still a target, reported down, so its alert fires; the runbook
  (docs/runbooks/platform.md) tells operators what to expect.

Opt-in, because it needs Docker::

    EXAMLOPS_PROMETHEUS_LIVE=1 .venv/bin/pytest tests/integration/test_prometheus_optional_targets_live.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"
OPTIONAL_JOBS = ("gateway", "gateway_authz", "vllm", "dataplane_bus_bridge")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EXAMLOPS_PROMETHEUS_LIVE") != "1", reason="set EXAMLOPS_PROMETHEUS_LIVE=1"
    ),
]


def _image(dockerfile: str) -> str:
    return re.search(r"^FROM (\S+)", (COMPOSE / dockerfile).read_text(), re.M).group(1)


def _envoy_image() -> str:
    return yaml.safe_load((COMPOSE / "docker-compose.yml").read_text())["services"]["gateway"][
        "image"
    ]


def _docker(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True
    ).stdout.strip()


def _query(base: str, promql: str) -> list[dict]:
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)["data"]["result"]


def _until(fn, timeout: float = 60.0):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - Prometheus still starting
            last = exc
        time.sleep(1)
    return last


@pytest.fixture(scope="module")
def stack():
    tag = uuid.uuid4().hex[:8]
    network, envoy, prom = f"exa-prom-{tag}", f"exa-prom-gw-{tag}", f"exa-prom-p-{tag}"
    cfg = Path(tempfile.mkdtemp(prefix="exa-prom-"))
    cfg.chmod(0o755)

    jobs = [
        job
        for job in yaml.safe_load((COMPOSE / "prometheus.yml").read_text())["scrape_configs"]
        if job["job_name"] in OPTIONAL_JOBS
    ]
    assert {j["job_name"] for j in jobs} == set(OPTIONAL_JOBS)
    for job in jobs:  # faster than production, same mechanism
        for sd in job.get("dns_sd_configs", []):
            sd["refresh_interval"] = "2s"
    (cfg / "prometheus.yml").write_text(
        yaml.safe_dump({"global": {"scrape_interval": "2s"}, "scrape_configs": jobs})
    )
    (cfg / "envoy.yaml").write_text((COMPOSE / "gateway" / "envoy.yaml").read_text())
    for f in cfg.iterdir():
        f.chmod(0o644)

    _docker("network", "create", network)
    try:
        _docker(
            "run", "-d", "--rm", "--name", envoy, "--network", network,
            "--network-alias", "gateway", "-v", f"{cfg}:/cfg:ro",
            _envoy_image(), "-c", "/cfg/envoy.yaml", "--log-level", "warn",
        )  # fmt: skip
        _docker(
            "run", "-d", "--rm", "--name", prom, "--network", network, "-p", "127.0.0.1::9090",
            "-v", f"{cfg}/prometheus.yml:/etc/prometheus/prometheus.yml:ro",
            _image("Dockerfile.prometheus"),
            "--config.file=/etc/prometheus/prometheus.yml",
        )  # fmt: skip
        port = _docker("port", prom, "9090/tcp").rsplit(":", 1)[1]
        yield {"base": f"http://127.0.0.1:{port}", "envoy": envoy}
    finally:
        _docker("rm", "-f", envoy, prom, check=False)
        _docker("network", "rm", network, check=False)


def test_a_running_opt_in_service_is_found_and_scraped(stack):
    up = _until(lambda: _query(stack["base"], 'up{job="gateway"} == 1'))
    assert isinstance(up, list) and up, up
    assert up[0]["metric"]["instance"].endswith(":9902")
    # Envoy's statistics really arrive, under the names the gateway alerts use.
    assert _until(lambda: _query(stack["base"], "envoy_http_ext_authz_error"))


def test_an_absent_opt_in_service_has_no_up_series_to_alert_on(stack):
    _until(lambda: _query(stack["base"], 'up{job="gateway"} == 1'))
    time.sleep(6)  # several refresh and scrape rounds
    for job in ("gateway_authz", "vllm", "dataplane_bus_bridge"):
        assert _query(stack["base"], f'up{{job="{job}"}}') == [], job
    assert _query(stack["base"], "up == 0") == []  # what TargetDown selects


def test_a_service_that_stops_is_reported_down_not_forgotten(stack):
    """Docker's resolver fails the lookup of a stopped container (it does not answer "no such
    name"), and Prometheus keeps the last good discovery result on a failed lookup. So a service
    that was running and dies stays a target with ``up == 0``: its down alert and ``TargetDown``
    fire, which is the point. The runbook says what follows: a service removed on purpose alerts
    until Prometheus restarts."""
    _until(lambda: _query(stack["base"], 'up{job="gateway"} == 1'))
    _docker("stop", stack["envoy"])
    down = _until(lambda: _query(stack["base"], 'up{job="gateway"} == 0'), timeout=30)
    assert isinstance(down, list) and down, down
    time.sleep(10)  # several DNS refreshes later, still there
    assert _query(stack["base"], 'up{job="gateway"} == 0')
