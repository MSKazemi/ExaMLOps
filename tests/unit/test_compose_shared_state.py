"""Every compose service that reads or writes platform state is wired to the one shared store.

The 2026-09-04 audit (X1) gave the bridge, control plane and agent the shared ``/state`` database
and missed ``ray-serving``. The inference router and the shadow reader in that container then opened
a private ``/app/platform.db`` that no writer ever touched, so every canary split and shadow set
through `exa serve traffic`, `exa serve shadow` or the dashboard configured nothing a replica could
see (plan P0.4 / finding B4). The backup sidecar carried the same half-wiring: ``PLATFORM_DB`` but
not the engine selection, so on Postgres it would have backed up a leftover file (P0.9 / B6).

Nothing flagged either, because each service is correct in isolation. This guard names the services
that touch platform state and holds each one to the whole contract — the file, the engine, the DSN
and the mount — so a new stateful service cannot be added half-wired either.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "platform/infra/docker-compose/docker-compose.yml"

# Services whose code imports `examlops.platform_db` / `examlops.data` at runtime.
STATEFUL = {
    "ray-serving": "inference router traffic splits + shadow config (serving/*)",
    "control-plane": "commands, approvals, admission, outbox",
    "dataplane": "source catalog, pull history and snapshot metadata (ADR 0130)",
    "agent": "Skipper platform_ops tools and audit",
    "dashboard": "every platform.db-backed console",
    "dataplane-bus-bridge": "drift / input telemetry + audit",
    "backup": "snapshots the platform datastore",
    "autopilot-follower": "runs autopilot cycles on retrain.run_completed (ADR 0124)",
    "skipper-watch": "raises watch alerts to the outbox and audit (ADR 0104, ADR 0124)",
    "gateway-authz": "serving-gateway virtual keys and per-tenant quota counters (ADR 0126)",
}
SHARED_DB = "/state/platform.db"


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _mounts_state(service: dict) -> bool:
    return any(":/state" in str(v) for v in service.get("volumes") or [])


def test_stateful_services_still_exist():
    missing = sorted(set(STATEFUL) - set(_services()))
    assert not missing, f"STATEFUL names services compose no longer defines: {missing}"


def test_every_stateful_service_is_wired_to_the_shared_store():
    problems = []
    for name in STATEFUL:
        service = _services()[name]
        env = _env(service)
        if env.get("PLATFORM_DB") != SHARED_DB:
            problems.append(
                f"{name}: PLATFORM_DB must be {SHARED_DB}, got {env.get('PLATFORM_DB')}"
            )
        for var in ("EXAMLOPS_DB_BACKEND", "EXAMLOPS_POSTGRES_DSN"):
            if var not in env:
                problems.append(f"{name}: missing {var} (it would ignore a Postgres engine)")
        if not _mounts_state(service):
            problems.append(f"{name}: does not mount the shared /state directory")
    assert not problems, "\n".join(problems)


def test_no_other_service_half_wires_platform_state():
    """A service outside STATEFUL that sets PLATFORM_DB belongs in STATEFUL, fully wired."""
    stray = sorted(
        name
        for name, service in _services().items()
        if name not in STATEFUL and "PLATFORM_DB" in _env(service)
    )
    assert not stray, f"add these to STATEFUL (and wire them fully): {stray}"
