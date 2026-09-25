"""Live hydration of a project's two pipeline surfaces (ADR 0092 decision 1).

``examlops.data.projects.get_project_pipelines`` builds both surfaces from the ``project_pipelines``
registry and project membership. This module adds the *live* half the ADR decided on:

* **Prefect training surface** — the Prefect deployments whose tags include ``project:<name>`` (the
  tag ``pipelines/deploy.py`` writes through ``examlops.project_scope.prefect_tags``), with each
  deployment's schedule(s), work pool and paused flag, the newest *started* flow run across them
  (state + time), and the project storage prefix (ADR 0091) as the artifact destination.
* **Ray Serve serving surface** — the serve app's ``GET /models`` hot set, filtered to the project
  (the entry's own ``project`` field, or membership when the serving runtime could not resolve
  one), giving per-model aliases and a health verdict.

Contract, in the order it matters:

1. **Explicit sources only.** Nothing is contacted unless the caller names a URL, or the
   environment does (``PREFECT_API_URL`` / ``RAY_SERVE_URL``). A default ``localhost`` guess would
   make a read of ``platform.db`` measure whatever happens to listen on the operator's machine.
   ``EXAMLOPS_PROJECT_PIPELINES_LIVE=0`` turns live hydration off everywhere.
2. **Fail-open to the registry.** An unreachable, slow, or malformed source leaves that surface as
   the registry built it, marked ``source: "registry"`` with the reason under ``live_error``.
   Never an exception out of this module.
3. **Bounded.** Per-request timeouts (``EXAMLOPS_PROJECT_PIPELINES_TIMEOUT``, default 2 s), Prefect
   deployments paged at 200 and capped at :data:`MAX_DEPLOYMENTS`, the serve hot set capped at
   :data:`MAX_SERVED_ENTRIES`.
4. **Write-through cache.** A successful live read upserts the surface's ``project_pipelines`` row
   (ref, status, schedule, last run), so the next read without a live source falls back to the
   last *observed* state instead of ``unknown``. It is a cache refresh, not an operator mutation,
   so it is logged, not audited.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_DEPLOYMENTS = 1000
MAX_SERVED_ENTRIES = 5000
_PAGE = 200
_DEFAULT_TIMEOUT = 2.0
#: Whole-read budget of the Prefect surface, in multiples of the per-request timeout. Paging can
#: issue up to 7 requests; without a total budget a slow server holds the caller (a dashboard
#: worker thread the BFF has already given up on) for 7x the timeout.
_PREFECT_BUDGET_REQUESTS = 3
_clock = time.monotonic

#: Prefect state types of a run that has actually started (a SCHEDULED/PENDING run is a promise).
_STARTED_STATES = ["RUNNING", "COMPLETED", "FAILED", "CRASHED", "CANCELLED", "CANCELLING", "PAUSED"]
_BAD_STATES = {"FAILED", "CRASHED"}

#: The Ray Serve application every project's serving surface lives in (``serving/ray_serving``).
SERVE_APP = "multi_model_server"


@dataclass(frozen=True)
class LiveSources:
    """Where to hydrate from. ``None`` for a URL means "do not contact that source"."""

    prefect_api_url: str | None = None  # Prefect REST base, e.g. ``http://orchestrator:4200/api``
    serve_url: str | None = None  # Ray Serve base, e.g. ``http://ray-serving:8001``
    timeout: float = _DEFAULT_TIMEOUT
    serving_token: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.prefect_api_url or self.serve_url)


def live_enabled() -> bool:
    """The global kill switch: ``EXAMLOPS_PROJECT_PIPELINES_LIVE`` falsy ⇒ never contact anything."""
    return os.getenv("EXAMLOPS_PROJECT_PIPELINES_LIVE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _timeout() -> float:
    try:
        value = float(os.getenv("EXAMLOPS_PROJECT_PIPELINES_TIMEOUT", "") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return min(max(value, 0.1), 30.0)


def prefect_api_base(url: str | None) -> str | None:
    """Normalise a Prefect URL to its REST base: the UI root gains ``/api``, an API URL is kept."""
    if not url:
        return None
    base = url.strip().rstrip("/")
    if not base:
        return None
    return base if base.endswith("/api") else f"{base}/api"


def sources(
    *,
    prefect_url: str | None = None,
    serve_url: str | None = None,
    serving_token: str = "",
) -> LiveSources:
    """Build :class:`LiveSources` from explicit URLs, honouring the kill switch."""
    if not live_enabled():
        return LiveSources()
    return LiveSources(
        prefect_api_url=prefect_api_base(prefect_url),
        serve_url=(serve_url or "").strip().rstrip("/") or None,
        timeout=_timeout(),
        serving_token=serving_token,
    )


def sources_from_env() -> LiveSources:
    """Sources the environment names explicitly (never a default guess — contract point 1)."""
    return sources(
        prefect_url=os.getenv("PREFECT_API_URL") or None,
        serve_url=os.getenv("RAY_SERVE_URL") or None,
        serving_token=os.getenv("EXAMLOPS_SERVING_TOKEN", ""),
    )


class LiveSourceError(RuntimeError):
    """A live source answered unusably (HTTP error, non-JSON, wrong shape)."""


# ── Prefect ───────────────────────────────────────────────────────────────────


def _crons(dep: dict[str, Any]) -> list[str]:
    """Every active cron/interval schedule of a deployment (Prefect 3 ``schedules`` + 2.x ``schedule``)."""
    out: list[str] = []
    entries = dep.get("schedules") or []
    if not entries and dep.get("schedule"):
        entries = [{"schedule": dep["schedule"], "active": True}]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("active") is False:
            continue
        sched = entry.get("schedule") or {}
        if sched.get("cron"):
            out.append(str(sched["cron"]))
        elif sched.get("interval") is not None:
            out.append(f"every {sched['interval']}s")
        elif sched.get("rrule"):
            out.append(str(sched["rrule"]))
    return out


def _post_json(client: httpx.Client, url: str, body: dict[str, Any]) -> Any:
    resp = client.post(url, json=body)
    if resp.status_code >= 400:
        raise LiveSourceError(f"{url} answered HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise LiveSourceError(f"{url} returned non-JSON") from exc


def _prefect_client(src: LiveSources) -> httpx.Client:
    from examlops.service_auth import prefect_headers

    return httpx.Client(
        headers={"Accept": "application/json", **prefect_headers()},
        timeout=httpx.Timeout(src.timeout),
    )


def fetch_prefect_surface(
    project: str,
    src: LiveSources,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Assemble the live Prefect training surface for ``project``. Raises on an unusable source."""
    if not src.prefect_api_url:
        raise LiveSourceError("no Prefect API URL configured")
    base = src.prefect_api_url
    own = client is None
    http = client or _prefect_client(src)
    deadline = _clock() + src.timeout * _PREFECT_BUDGET_REQUESTS

    def post(url: str, body: dict[str, Any]) -> Any:
        if _clock() >= deadline:
            raise LiveSourceError(
                f"Prefect read exceeded its {src.timeout * _PREFECT_BUDGET_REQUESTS:g}s budget"
            )
        return _post_json(http, url, body)

    try:
        tag = f"project:{project}"
        deployments: list[dict[str, Any]] = []
        offset = 0
        more = False  # did the last page come back full, i.e. may rows remain past the cap?
        while len(deployments) < MAX_DEPLOYMENTS:
            page = post(
                f"{base}/deployments/filter",
                {
                    "deployments": {"tags": {"all_": [tag]}},
                    "sort": "NAME_ASC",
                    "limit": _PAGE,
                    "offset": offset,
                },
            )
            if not isinstance(page, list):
                raise LiveSourceError("Prefect /deployments/filter did not return a list")
            deployments.extend(d for d in page if isinstance(d, dict))
            more = len(page) >= _PAGE
            if not more:
                break
            offset += _PAGE
        if len(deployments) > MAX_DEPLOYMENTS:
            more = True  # rows past the cap are already in hand
        elif more and len(deployments) == MAX_DEPLOYMENTS:
            # The cap landed exactly on a full page: ask for one row past it rather than guess, so
            # a project with exactly MAX_DEPLOYMENTS deployments is not reported as truncated.
            probe = post(
                f"{base}/deployments/filter",
                {
                    "deployments": {"tags": {"all_": [tag]}},
                    "sort": "NAME_ASC",
                    "limit": 1,
                    "offset": MAX_DEPLOYMENTS,
                },
            )
            more = isinstance(probe, list) and len(probe) > 0
        truncated = more and len(deployments) >= MAX_DEPLOYMENTS
        deployments = deployments[:MAX_DEPLOYMENTS]

        details: list[dict[str, Any]] = [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "schedules": _crons(d),
                "work_pool": d.get("work_pool_name"),
                "paused": bool(d.get("paused")),
                "status": d.get("status"),
            }
            for d in deployments
        ]
        ids = [d["id"] for d in details if d.get("id")]
        last_run: dict[str, Any] | None = None
        if ids:
            runs = post(
                f"{base}/flow_runs/filter",
                {
                    "flow_runs": {
                        "deployment_id": {"any_": ids},
                        "state": {"type": {"any_": _STARTED_STATES}},
                        # START_TIME_DESC sorts on coalesce(start_time, expected_start_time): a
                        # scheduled run cancelled before it started (CANCELLED, no start_time,
                        # future expected time) would otherwise sort first and mask the real
                        # last run — e.g. a FAILED one, turning "degraded" into "healthy".
                        "start_time": {"is_null_": False},
                    },
                    "sort": "START_TIME_DESC",
                    "limit": 1,
                },
            )
            if not isinstance(runs, list):
                raise LiveSourceError("Prefect /flow_runs/filter did not return a list")
            if runs and isinstance(runs[0], dict):
                last_run = runs[0]
    finally:
        if own:
            http.close()

    by_id = {d["id"]: d["name"] for d in details}
    schedules = sorted({c for d in details for c in d["schedules"]})
    pools = sorted({str(d["work_pool"]) for d in details if d.get("work_pool")})
    last_state = (last_run or {}).get("state_type")
    if not details:
        status = "unknown"
    elif last_state in _BAD_STATES or all(d["paused"] for d in details):
        status = "degraded"
    elif last_state:
        status = "healthy"
    else:
        status = "unknown"  # deployed but never run: nothing observed yet
    return {
        "source": "live",
        "ref": tag,
        "deployments": [d["name"] for d in details],
        "deployment_details": details,
        "schedule": ", ".join(schedules) or None,
        "work_pool": ", ".join(pools) or None,
        "last_run_at": (last_run or {}).get("start_time"),
        "last_run_state": last_state,
        "last_run_deployment": by_id.get((last_run or {}).get("deployment_id")),
        "status": status,
        "truncated": truncated,
    }


# ── Ray Serve ─────────────────────────────────────────────────────────────────


def fetch_rayserve_surface(
    project: str,
    models: list[str],
    src: LiveSources,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Assemble the live Ray Serve surface from the serve app's ``/models``. Raises when unusable."""
    if not src.serve_url:
        raise LiveSourceError("no Ray Serve URL configured")
    url = f"{src.serve_url}/models"
    headers = {"Accept": "application/json"}
    if src.serving_token.strip():
        headers["Authorization"] = f"Bearer {src.serving_token.strip()}"
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(src.timeout))
    try:
        resp = http.get(url, headers=headers)
    finally:
        if own:
            http.close()
    if resp.status_code >= 400:
        raise LiveSourceError(f"{url} answered HTTP {resp.status_code}")
    try:
        entries = resp.json()
    except ValueError as exc:
        raise LiveSourceError(f"{url} returned non-JSON") from exc
    if not isinstance(entries, list):
        raise LiveSourceError(f"{url} did not return a list")

    members = {m.lower(): m for m in models}
    aliases: dict[str, list[str]] = {}
    health: dict[str, str] = {}
    for entry in entries[:MAX_SERVED_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("model_name") or "")
        owner = entry.get("project")
        # The serve app resolves the owner itself; when it could not (no datastore in the serving
        # runtime ⇒ ``None``) membership decides. An entry claimed by ANOTHER project never counts.
        if owner not in (None, "") and owner != project:
            continue
        if owner in (None, "") and name.lower() not in members:
            continue
        display = members.get(name.lower(), name)
        if entry.get("alias"):
            aliases.setdefault(display, [])
            if entry["alias"] not in aliases[display]:
                aliases[display].append(str(entry["alias"]))
        ok = str(entry.get("status") or "").lower() == "ok"
        # A model is healthy only when every hot (model, alias) it has is healthy.
        health[display] = "ok" if ok and health.get(display, "ok") == "ok" else "error"

    served = sorted(health)
    unserved = sorted(m for m in models if m not in health)
    if not models and not served:
        status = "unknown"
    elif any(v != "ok" for v in health.values()) or unserved:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "source": "live",
        "ref": SERVE_APP,
        "served": served,
        "unserved": unserved,
        "aliases": {k: sorted(v) for k, v in sorted(aliases.items())},
        "health": dict(sorted(health.items())),
        "status": status,
    }


# ── merge ─────────────────────────────────────────────────────────────────────


def _storage_prefix(project: str) -> str | None:
    try:
        from examlops.data.projects import get_project_storage

        rec = get_project_storage(project)
    except Exception:  # noqa: BLE001 — storage is decoration here; fail-open
        return None
    return f"s3://{rec['bucket']}/{rec['prefix']}" if rec else None


def _cache(project: str, kind: str, surface: dict[str, Any]) -> None:
    try:
        from examlops.data.projects import upsert_project_pipeline

        upsert_project_pipeline(
            project,
            kind,
            str(surface.get("ref") or ""),
            status=str(surface.get("status") or "unknown"),
            schedule=surface.get("schedule"),
            last_run_at=surface.get("last_run_at"),
        )
    except Exception as exc:  # noqa: BLE001 — a cache write must never break a read
        log.warning("project_pipelines: cache write failed for %s/%s: %s", project, kind, exc)


def hydrate(
    project: str,
    models: list[str],
    base: dict[str, Any],
    src: LiveSources,
    *,
    prefect_client: httpx.Client | None = None,
    serve_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Overlay live detail on the registry-built ``base`` surfaces. Never raises.

    ``base`` is ``{"prefect": {...}|None, "rayserve": {...}|None}`` as the registry built it. A
    surface a live source answered for becomes ``source: "live"`` (and is cached); one whose source
    failed keeps its registry values with ``source: "registry"`` and a ``live_error``.
    """
    out: dict[str, Any] = {
        "prefect": dict(base["prefect"]) if base.get("prefect") else None,
        "rayserve": dict(base["rayserve"]) if base.get("rayserve") else None,
    }
    for key in ("prefect", "rayserve"):
        if out[key] is not None:
            out[key].setdefault("source", "registry")

    prefix = _storage_prefix(project)
    if out["prefect"] is not None and prefix:
        out["prefect"].setdefault("storage_prefix", prefix)

    if src.prefect_api_url:
        try:
            live = fetch_prefect_surface(project, src, client=prefect_client)
        except Exception as exc:  # noqa: BLE001 — fail-open to the registry (contract point 2)
            log.warning("project_pipelines: Prefect live read failed for %s: %s", project, exc)
            if out["prefect"] is not None:
                out["prefect"]["live_error"] = str(exc)[:200]
        else:
            if live["deployments"] or out["prefect"] is not None:
                merged = {**(out["prefect"] or {}), **live}
                merged.pop("live_error", None)
                if prefix:
                    merged["storage_prefix"] = prefix
                out["prefect"] = merged
                _cache(project, "prefect", live)

    if src.serve_url:
        try:
            live = fetch_rayserve_surface(project, models, src, client=serve_client)
        except Exception as exc:  # noqa: BLE001 — fail-open to the registry (contract point 2)
            log.warning("project_pipelines: Ray Serve live read failed for %s: %s", project, exc)
            if out["rayserve"] is not None:
                out["rayserve"]["live_error"] = str(exc)[:200]
        else:
            if live["served"] or out["rayserve"] is not None:
                merged = {**(out["rayserve"] or {"models": list(models), "traffic": {}}), **live}
                merged.pop("live_error", None)
                out["rayserve"] = merged
                _cache(project, "rayserve", live)
    return out


__all__ = [
    "MAX_DEPLOYMENTS",
    "MAX_SERVED_ENTRIES",
    "SERVE_APP",
    "LiveSourceError",
    "LiveSources",
    "fetch_prefect_surface",
    "fetch_rayserve_surface",
    "hydrate",
    "live_enabled",
    "prefect_api_base",
    "sources",
    "sources_from_env",
]
