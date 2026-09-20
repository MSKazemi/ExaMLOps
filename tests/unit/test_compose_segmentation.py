"""The segmented network overlay cuts nothing a service uses, and isolates what it promises (P3.5).

``docker-compose.segmented.yml`` replaces one flat network with zones. A zone that is missing a
member breaks a service silently at runtime — the connection simply times out — so this guard
derives every service-to-service dependency the stack has and proves each one still shares a
network: from the compose environment (URLs, ``host:port``, ``PGHOST``), the ``.env`` template
services load, ``depends_on``, the Prometheus/Grafana/Promtail configs, the environment JupyterHub
gives notebooks, and the dependencies only runtime configuration creates (the NATS backbone, the
Postgres backend). It then holds the overlay to its security claims: a notebook reaches only what a
notebook needs, and nothing but the dashboard reaches the Docker API.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

D = Path(__file__).resolve().parents[2] / "platform" / "infra" / "docker-compose"
BASE = yaml.safe_load((D / "docker-compose.yml").read_text(encoding="utf-8"))
OVERLAY = yaml.safe_load((D / "docker-compose.segmented.yml").read_text(encoding="utf-8"))
SERVICES = set(BASE["services"])
NOTEBOOK = "<spawned notebook>"

# Edges that only runtime configuration creates, so no static file names them.
RUNTIME_EDGES = {
    # EXAMLOPS_NATS_URL (set in .env when the backbone is on): the relay, the live stream, the
    # serving snapshot's KV reads and the two consumers.
    ("control-plane", "nats"),
    ("dashboard", "nats"),
    ("ray-serving", "nats"),
    ("autopilot-follower", "nats"),
    ("skipper-watch", "nats"),
    # EXAMLOPS_OPENLINEAGE_URL=http://marquez:5000 (set in .env with `--profile lineage`): the
    # processes that emit lineage in a container — dataplane pulls, the dashboard's CLI console,
    # the control plane (ADR 0004 clause 3).
    ("dataplane", "marquez"),
    ("dashboard", "marquez"),
    ("control-plane", "marquez"),
}
# With EXAMLOPS_DB_BACKEND=postgres every service holding platform state talks to Postgres.
STATEFUL = {
    "ray-serving",
    "control-plane",
    "dataplane",
    "agent",
    "dashboard",
    "dataplane-bus-bridge",
    "backup",
    "autopilot-follower",
    "skipper-watch",
    "gateway-authz",
}
RUNTIME_EDGES |= {(s, "postgres") for s in STATEFUL}

_HOSTS = "|".join(sorted(map(re.escape, SERVICES), key=len, reverse=True))
_URL_HOST = re.compile(rf"(?:://|@)({_HOSTS})(?=[:/\"'\s]|$)")
_HOST_PORT = re.compile(rf"(?:^|[\s,\"'\[])({_HOSTS}):\d+")


def _hosts_in(text: str) -> set[str]:
    return {m.group(1) for m in _URL_HOST.finditer(text)} | {
        m.group(1) for m in _HOST_PORT.finditer(text)
    }


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _env_template_hosts() -> set[str]:
    """Hosts named in this directory's ``.env.example`` — deliberately private
    (``.dualgit/public.carveout``: it duplicates the root template and documents
    site-specific defaults). Absent on a public-only checkout; contributes nothing
    to the graph there rather than failing, same as a service with no ``.env`` values."""
    path = D / ".env.example"
    if not path.exists():
        return set()
    hosts: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            hosts |= _hosts_in(line.split("=", 1)[1])
    return hosts


def dependencies() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set(RUNTIME_EDGES)
    template = _env_template_hosts()
    for name, svc in BASE["services"].items():
        env = _env(svc)
        deps = set().union(*(_hosts_in(v) for v in env.values())) if env else set()
        for key in ("PGHOST", "DOCKER_HOST"):
            if key in env:
                deps |= {s for s in SERVICES if s in env[key]}
        files = svc.get("env_file") or []
        files = files if isinstance(files, list) else [files]
        if any((f["path"] if isinstance(f, dict) else f) == ".env" for f in files):
            deps |= template
        on = svc.get("depends_on") or {}
        deps |= set(on if isinstance(on, list) else on)
        edges |= {(name, dep) for dep in deps if dep != name}
    prometheus = (D / "prometheus.yml").read_text(encoding="utf-8")
    edges |= {("prometheus", h) for h in _hosts_in(prometheus)}
    # Services found by DNS name the host and the port separately, so no `host:port` shows them.
    for job in yaml.safe_load(prometheus)["scrape_configs"]:
        for sd in job.get("dns_sd_configs", []):
            edges |= {("prometheus", h) for h in sd["names"]}
    for datasource in (D / "grafana" / "provisioning" / "datasources").glob("*.yml"):
        edges |= {("grafana", h) for h in _hosts_in(datasource.read_text(encoding="utf-8"))}
    edges |= {("promtail", h) for h in _hosts_in((D / "promtail-config.yml").read_text())}
    hub = (D / "jupyterhub_config.py").read_text(encoding="utf-8")
    spawner_env = hub.split("c.DockerSpawner.environment", 1)[1].split("}", 1)[0]
    edges |= {(NOTEBOOK, h) for h in _hosts_in(spawner_env)}
    return {(a, b) for a, b in edges if b in SERVICES}


def networks_of(name: str) -> set[str]:
    if name == NOTEBOOK:
        return {"notebooks"}
    base = BASE["services"][name].get("networks") or []
    extra = OVERLAY["services"].get(name, {}).get("networks") or []
    return set(base if isinstance(base, list) else base) | set(extra)


def reachable_from(name: str) -> set[str]:
    mine = networks_of(name)
    return {other for other in SERVICES if other != name and networks_of(other) & mine}


# ─── nothing a service uses is cut ────────────────────────────────────────────


def test_the_dependency_scan_sees_the_known_edges():
    """Guard the guard: an extractor that stopped matching would pass every assertion below."""
    edges = dependencies()
    for edge in (
        ("control-plane", "orchestrator"),
        ("mlflow", "postgres"),
        ("mlflow", "minio"),
        ("dashboard", "docker-socket-proxy"),
        ("prometheus", "ray-serving"),
        ("prometheus", "gateway-authz"),  # found by DNS, not a static target
        ("grafana", "tempo"),
        ("promtail", "loki"),
        ("dataplane-bus-bridge", "control-plane"),
        (NOTEBOOK, "mlflow"),
        (NOTEBOOK, "minio"),
        ("ray-serving", "nats"),
    ):
        assert edge in edges, edge
    assert len(edges) >= 60


def test_every_service_has_a_zone():
    missing = sorted(SERVICES - set(OVERLAY["services"]))
    assert not missing, f"services the segmented overlay does not place: {missing}"


def test_every_dependency_shares_a_network():
    cut = sorted(f"{a} → {b}" for a, b in dependencies() if not networks_of(a) & networks_of(b))
    assert not cut, "the segmented overlay cuts these dependencies:\n" + "\n".join(cut)


def test_notebooks_are_spawned_into_their_zone():
    env = OVERLAY["services"]["jupyterhub"]["environment"]
    assert env["DOCKER_NETWORK_NAME"] == f"{BASE['name']}_notebooks"


# ─── what it promises to isolate ──────────────────────────────────────────────


def test_a_notebook_reaches_only_what_a_notebook_needs():
    """A notebook runs arbitrary user code."""
    reach = reachable_from(NOTEBOOK)
    assert reach == {
        "jupyterhub",
        "mlflow",
        "minio",
        "orchestrator",
        "ray-serving",
        "control-plane",
    }
    for forbidden in ("postgres", "docker-socket-proxy", "nats", "dashboard", "agent", "backup"):
        assert forbidden not in reach


def test_only_the_dashboard_reaches_the_docker_api():
    assert reachable_from("docker-socket-proxy") == {"dashboard"}


def test_postgres_is_reachable_only_by_its_clients():
    clients = {a for a, b in dependencies() if b == "postgres"}
    assert reachable_from("postgres") <= clients | {"postgres"}


def test_the_zones_that_need_no_internet_have_none():
    nets = OVERLAY["networks"]
    assert all(nets[n].get("internal") is True for n in ("db", "objects", "docker-api"))
