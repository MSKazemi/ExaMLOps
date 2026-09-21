# tests/unit/test_every_service_is_described.py
"""Every service the stack runs is described on the page that claims to list them.

`docs/explore/services.md` is *Services and their jobs* — the page an operator reads to find out what
is running and why. **Four** services were missing from it, and the omissions were not random:
**NATS JetStream**, the event backbone the whole platform publishes through; the **serving-gateway
authorization service**, which decides whether an inference request is allowed and fails closed when
it cannot tell; the **autopilot follower**, which reacts to drift without polling; and the
**Postgres role provisioning** step that gives each service its own database. A component that
decides access, or carries every event, and is absent from the page listing components is the kind of
gap that only shows up when someone is trying to debug it at 3 a.m.

The Compose file is the definition — it is what actually runs — and this holds the page to it.

**Names are matched through an explicit map, not by string search.** The page uses human headings
("MinIO bucket init (one-shot)" for `minio-init`), so searching for the Compose name over-reports
absences: that instrument claimed four gaps where there were three. A map makes each pairing a
decision somebody made rather than an accident of wording.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "platform" / "infra" / "docker-compose" / "docker-compose.yml"
PAGE = ROOT / "docs" / "explore" / "services.md"

#: Compose service → a phrase that must appear in one of the page's `###` headings. Where the page
#: names a component the way a human would, the pairing is recorded here rather than guessed.
HEADING_FOR = {
    "minio-init": "MinIO bucket init",
    "postgres-init": "Postgres role provisioning",
    "gateway-authz": "authorization service",
    "autopilot-follower": "Autopilot follower",
    "ray-serving": "Ray Serve",
    "orchestrator": "Prefect server",
    "control-plane": "Control plane",
    "mlflow": "MLflow",
    "minio": "MinIO object store",
    "postgres": "PostgreSQL",
    "dashboard": "Dashboard",
    "agent": "Skipper agent server",
    "backup": "Backup sidecar",
    "marquez": "Marquez",
    "dataplane": "Dataplane service",
    "llm-gateway": "LLM gateway",
    "jupyterhub": "JupyterHub",
    "docker-socket-proxy": "Docker socket proxy",
    "dataplane-bus-bridge": "bridge",
    "vllm": "vLLM",
    "nats": "NATS",
}

#: Services deliberately not given their own section, with the reason.
NOT_DESCRIBED = {
    "promtail": "a log shipper for the monitoring stack, covered by the observability guide",
    "prometheus": "monitoring stack, described in the observability guide",
    "grafana": "monitoring stack, described in the observability guide",
    "loki": "monitoring stack, described in the observability guide",
    "tempo": "monitoring stack, described in the observability guide",
    "alertmanager": "monitoring stack, described in the observability guide",
    "kafka": "dev-only single-node broker behind the `kafka` profile",
    "marquez-db": "the Marquez deployment's own Postgres, described in its section",
    "marquez-web": "the Marquez deployment's UI, described in its section",
    "skipper-watch": "described as the Skipper-watch monitoring daemon",
}


def _compose_services() -> set[str]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = set(data.get("services") or {})
    assert len(services) > 15, f"only {len(services)} services parsed — the compose path is stale"
    return services


def _headings() -> str:
    text = PAGE.read_text(encoding="utf-8")
    headings = "\n".join(re.findall(r"^###\s+(.+)$", text, re.M))
    assert headings.count("\n") > 20, "too few sections found — the page's format changed"
    return headings


def test_every_compose_service_is_described_or_exempted():
    services = _compose_services()
    headings = _headings()
    undescribed = []
    for service in sorted(services):
        if service in NOT_DESCRIBED:
            continue
        phrase = HEADING_FOR.get(service, service)
        if phrase.lower() not in headings.lower():
            undescribed.append(f"{service} (looked for {phrase!r} in a heading)")
    assert not undescribed, (
        "the stack runs these and `docs/explore/services.md` does not describe them:\n  "
        + "\n  ".join(undescribed)
        + "\nAdd a section, or add it to NOT_DESCRIBED with the reason it needs none."
    )


def test_the_name_map_and_exemptions_still_match_the_stack():
    """A mapping for a service that no longer exists sends the next reader looking for nothing."""
    services = _compose_services()
    stale_map = sorted(set(HEADING_FOR) - services)
    stale_exempt = sorted(set(NOT_DESCRIBED) - services)
    assert not stale_map, f"HEADING_FOR names services the stack no longer runs: {stale_map}"
    assert not stale_exempt, f"NOT_DESCRIBED names services that are gone: {stale_exempt}"


def test_the_page_still_has_the_shape_this_guard_reads():
    """If the `###`-per-service format ever changes, this guard must fail loudly rather than pass
    by finding nothing — the failure mode every scan in this directory is written against."""
    assert "authorization service" in _headings()
