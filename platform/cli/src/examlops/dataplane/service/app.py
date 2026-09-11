"""Dataplane HTTP service (ADR 0130 §9). Every route is a library call; no logic lives here.

Container port 8010 (host 18010, loopback-only on n1). Auth is in :mod:`.auth`: the static
``DATAPLANE_TOKEN`` and, when a trust file is configured, federated data-center tokens (ADR 0120).
Pulls run on the :class:`~.scheduler.Scheduler`'s worker pool through ``run_pull``; the scheduler
also runs each source on its ``schedule``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from examlops import dataplane
from examlops.data import dataplane as catalog
from examlops.dataplane.safety import redact
from examlops.dataplane.service.auth import (
    FORBIDDEN_DETAIL,
    Caller,
    auth_mode,
    authenticate_read,
    authenticate_write,
    authorize_source,
    may_access_project,
    require_read,
)
from examlops.dataplane.service.scheduler import Scheduler, parse_interval, parse_timestamp
from examlops.dataplane.types import DataplaneError, Limits, SpecError

logger = logging.getLogger(__name__)

_COMMITTED = ("succeeded", "unchanged")
_ACTIVE = ("running", "committing")
# How long /health waits for the snapshot store before reporting it down.
_STORE_PROBE_TIMEOUT_S = 3.0


class SourceBody(BaseModel):
    """``PUT /sources/{name}`` — the same options as ``exa dataplane sources create``.

    Unknown keys are refused: a credential typed at the top level must not be silently dropped
    (or, worse, accepted by a later version). Credentials live in Named Connections.
    """

    model_config = ConfigDict(extra="forbid")

    connector: str
    project: str = ""
    connection: str | None = None
    spec: dict[str, Any] = {}
    schedule: str | None = None
    limits: dict[str, Any] | None = None
    contract: str | None = None
    enabled: bool = True


class PullBody(BaseModel):
    """``POST /sources/{name}/pull`` — what ``exa dataplane pull --remote`` sends."""

    model_config = ConfigDict(extra="forbid")

    project: str = ""
    full: bool = False


def _source(s: Any) -> dict[str, Any]:
    return {
        "project": s.project,
        "name": s.name,
        "connector": s.connector,
        "connection": s.connection,
        "schedule": s.schedule,
        "enabled": s.enabled,
        "spec": s.spec,
        "limits": s.limits.to_dict(),
        "contract": s.contract,
    }


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, redact(str(exc)))


def _existing(name: str, project: str) -> Any:
    try:
        return dataplane.get_source_def(name, project)
    except SpecError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, redact(str(exc))) from None


def _actor(caller: Caller | None) -> str | None:
    return caller.actor if caller is not None else None


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 with only ``loc``/``type``/``msg`` per error.

    FastAPI's default body echoes each rejected ``input`` (plus ``ctx``/``url``) — a credential
    typed into a refused field (``{"token": "…"}``) would come straight back in the response.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [
        {
            "loc": [str(part) for part in e.get("loc", ())],
            "type": str(e.get("type", "")),
            "msg": redact(str(e.get("msg", ""))),
        }
        for e in errors
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": detail}
    )


class _StoreProbe:
    """At most one store probe in flight, and a bounded wait for it, so ``/health`` cannot hang
    on an unreachable object store (botocore retries can take minutes)."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._box: dict[str, bool] = {}

    @staticmethod
    def _probe(box: dict[str, bool]) -> None:
        try:
            s = dataplane.store_from_env()
            # The bucket (the root's first segment) must exist. A missing prefix under it is only a
            # fresh store, but a missing bucket fails every publish — and `exists(root)` alone
            # reads both as False, so this used to report a store that cannot be written as OK.
            # (For a local-path store the first segment is a top-level directory: a no-op check.)
            stripped = s.root.lstrip("/")
            bucket = s.root[: len(s.root) - len(stripped)] + stripped.split("/", 1)[0]
            if not s.fs.exists(bucket):
                box["ok"] = False
                return
            if s.fs.exists(s.root):
                s.fs.ls(s.root)
            box["ok"] = True
        except Exception:  # noqa: BLE001 — reported as a component state, never raised
            box["ok"] = False

    def check(self, timeout: float) -> bool:
        with self._mu:
            if self._thread is None or not self._thread.is_alive():
                self._box = {}
                self._thread = threading.Thread(
                    target=self._probe, args=(self._box,), name="dataplane-store-probe", daemon=True
                )
                self._thread.start()
            # else: the previous probe is still hanging — wait on it rather than pile up another
            thread, box = self._thread, self._box
        thread.join(timeout)
        return not thread.is_alive() and box.get("ok", False)


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None


def _freshness_collector() -> Any:
    """Per-source gauges computed at scrape time from the catalog (never cached, never stale)."""
    from prometheus_client.core import GaugeMetricFamily, Metric
    from prometheus_client.registry import Collector

    class _Collector(Collector):
        def collect(self) -> Iterable[Metric]:
            fresh = GaugeMetricFamily(
                "dataplane_source_freshness_seconds",
                # last_pull(committed_only=True) counts `unchanged` pulls, and they stamp
                # finished_at: this is "last successful pull", not "last new snapshot".
                "Seconds since the source's last successful pull (succeeded or unchanged)",
                labels=["source"],
            )
            up = GaugeMetricFamily(
                "dataplane_source_up",
                "1 if the source's last finished pull committed (succeeded or unchanged); 0 if "
                "it failed, or if the pull catalog could not be read for this source",
                labels=["source"],
            )
            # Always emitted, with or without registered sources: `dataplane_source_up` only ever
            # has a series per *registered* source, so it cannot say anything about a catalog that
            # cannot be read at all (an install with zero sources would otherwise look identical
            # to one whose catalog is down). `DataplaneCatalogUnavailable` alerts on this instead.
            catalog_up = GaugeMetricFamily(
                "dataplane_catalog_up",
                "1 if the source catalog answered list_source_defs() this scrape, else 0",
            )
            # Final review I10: what `DataplaneSourceStale` compares freshness against (2 × this).
            # Only a source the scheduler actually runs gets a series — enabled, with a schedule
            # that parses — so an unscheduled or disabled source (pulled only on demand) never
            # reads as stale: the alert's `on(source)` join has nothing to match for it.
            schedule = GaugeMetricFamily(
                "dataplane_source_schedule_seconds",
                "The pull interval of an enabled, scheduled source, in seconds (from its schedule)",
                labels=["source"],
            )
            now = time.time()
            try:
                sources = dataplane.list_source_defs()
                catalog_up.add_metric([], 1.0)
            except Exception as exc:  # noqa: BLE001 — a scrape must not fail on the datastore
                logger.warning("dataplane metrics: catalog unavailable: %s", type(exc).__name__)
                sources = []
                catalog_up.add_metric([], 0.0)
            for src in sources:
                if src.enabled and src.schedule:
                    try:
                        schedule.add_metric([src.key], float(parse_interval(src.schedule)))
                    except SpecError:
                        pass  # the scheduler skips it too (and warns), so it is never pulled
                try:
                    recent = catalog.list_pulls(project=src.project, source=src.name, limit=10)
                    committed = catalog.last_pull(src.project, src.name)
                except Exception:  # noqa: BLE001
                    # A registered source always gets a series, even when its own read fails: it
                    # reads as a failing pull (0), not as an absent one — absence of the whole
                    # series means "not a registered source", which DataplanePullFailing relies on.
                    up.add_metric([src.key], 0.0)
                    continue
                # A pull in progress says nothing about health yet: judge the last finished one.
                finished = next((r for r in recent if r["status"] not in _ACTIVE), None)
                up.add_metric(
                    [src.key], 1.0 if finished and finished["status"] in _COMMITTED else 0.0
                )
                ts = parse_timestamp(committed.get("finished_at")) if committed else None
                if ts is not None:
                    fresh.add_metric([src.key], max(0.0, now - ts))
            yield fresh
            yield up
            yield catalog_up
            yield schedule

    return _Collector()


# Third-party HTTP clients log each request at INFO *with its full URL* — userinfo and signed query
# strings included (`HTTP Request: GET https://user:pw@host/x?sig=…`), which is exactly what the
# connectors' redaction exists to keep out of logs. Pinned to WARNING even if root is lowered.
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore", "urllib3", "botocore", "s3fs", "fsspec", "aiohttp")


def configure_logging() -> None:
    """Container logging for the service: root at WARNING, ``examlops`` at INFO.

    Only this platform's own records (the startup auth-mode line, pull failures) are raised to
    INFO; every other library stays at WARNING, and the HTTP clients that log URLs are pinned
    there explicitly. Adds a stream handler only when root has none, so a host that configured
    logging keeps its handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("examlops").setLevel(logging.INFO)
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def create_app(*, start_scheduler: bool = True) -> FastAPI:
    from prometheus_client import CollectorRegistry

    from examlops.dataplane.metrics import _metrics

    scheduler = Scheduler(
        interval_s=_env_number("EXAMLOPS_DATAPLANE_SCHEDULER_INTERVAL", 30.0),
        workers=int(_env_number("EXAMLOPS_DATAPLANE_WORKERS", 2)),
    )
    # The per-source gauges live in this app's own registry (so creating several apps — tests —
    # never collides); the pull counters live in the default one. /metrics serves both.
    gauges = CollectorRegistry(auto_describe=False)
    gauges.register(_freshness_collector())
    _metrics()  # declare dataplane_pull_total & co. up front, so they exist before the first pull

    store_probe = _StoreProbe()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mode = auth_mode()  # never carries the token or the trust file's parse error
        if mode == "open" or "invalid" in mode:
            logger.warning("dataplane: auth mode %s", mode)
        else:
            logger.info("dataplane: auth mode %s", mode)
        # Task 22a: a process that was OOM-killed or crashed mid-pull leaves its catalog row
        # `running` forever and its stage dir behind — clean both up before serving traffic.
        from examlops.dataplane.pull import cleanup_stale_stage_dirs, reap_interrupted_pulls

        try:
            await asyncio.to_thread(reap_interrupted_pulls)
        except Exception:  # noqa: BLE001 — a datastore hiccup at startup must not crash the app
            logger.warning("dataplane: could not reap interrupted pulls at startup", exc_info=True)
        try:
            await asyncio.to_thread(cleanup_stale_stage_dirs)
        except Exception:  # noqa: BLE001
            logger.warning("dataplane: could not clean stale stage dirs at startup", exc_info=True)
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            # stop() waits (bounded) for running pulls; keep that off the event loop.
            await asyncio.to_thread(scheduler.stop)

    app = FastAPI(title="ExaMLOps dataplane", version="1", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.add_exception_handler(RequestValidationError, _validation_error)

    @app.get("/health")
    def health() -> dict[str, Any]:
        from examlops.dataplane.connectors import registry

        db_ok = True
        try:
            catalog.list_sources()
        except Exception:  # noqa: BLE001 — reported as a component state, never raised
            db_ok = False
        store_ok = store_probe.check(_STORE_PROBE_TIMEOUT_S)
        connectors: dict[str, bool] = {}
        try:
            for c in registry.all_connectors():
                try:
                    connectors[c.kind] = bool(c.available()[0])
                except Exception:  # noqa: BLE001
                    connectors[c.kind] = False
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "ok" if db_ok and store_ok else "degraded",
            "db": db_ok,
            "store": store_ok,
            "auth": auth_mode(),
            "connectors": connectors,
        }

    @app.get("/ready")
    def ready() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/metrics")
    def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

        return Response(
            generate_latest(REGISTRY) + generate_latest(gauges), media_type=CONTENT_TYPE_LATEST
        )

    @app.get("/connectors", dependencies=[Depends(require_read)])
    def connectors() -> list[dict[str, Any]]:
        from examlops.dataplane.connectors import registry

        rows = []
        for c in registry.all_connectors():
            ok, why = c.available()
            rows.append(
                {
                    "kind": c.kind,
                    "available": ok,
                    "detail": why,
                    "connection_kinds": list(c.connection_kinds),
                    "incremental": c.supports_incremental,
                    "extra": c.extra,
                }
            )
        return rows

    # Source-scoped routes authenticate in the dependency and authorise in the body, once the
    # source's project is known (for PUT and pull it sits in the request body): the project check
    # (spec §10) and the center's PDP with the real project run BEFORE the source is looked up,
    # so a caller without access gets one 403 whether or not the source exists (final review I12).

    @app.get("/sources")
    def sources(
        project: str | None = None, caller: Caller | None = Depends(require_read)
    ) -> list[dict[str, Any]]:
        if project is not None and not may_access_project(caller, "read", project):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL)
        # One non-auditing check per distinct project, not one audited check per source.
        visible: dict[str, bool] = {}

        def _can_see(p: str) -> bool:
            if p not in visible:
                visible[p] = may_access_project(caller, "read", p, audit=False)
            return visible[p]

        return [_source(s) for s in dataplane.list_source_defs(project) if _can_see(s.project)]

    @app.get("/sources/{name}")
    def source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller | None = Depends(authenticate_read),
    ) -> dict[str, Any]:
        authorize_source(caller, request, "read", project, name)
        return _source(_existing(name, project))

    @app.put("/sources/{name}")
    def put_source(
        name: str, body: SourceBody, request: Request, caller: Caller = Depends(authenticate_write)
    ) -> dict[str, Any]:
        authorize_source(caller, request, "write", body.project, name)
        try:
            s = dataplane.define_source(
                name,
                body.connector,
                project=body.project,
                connection=body.connection,
                spec=body.spec,
                schedule=body.schedule,
                limits=Limits.from_dict(body.limits),
                contract=body.contract,
                enabled=body.enabled,
                actor=caller.actor,
            )
        except (DataplaneError, ValueError, TypeError) as exc:
            raise _bad_request(exc) from None
        return _source(s)

    @app.delete("/sources/{name}")
    def delete_source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        authorize_source(caller, request, "write", project, name)
        try:
            removed = dataplane.remove_source(name, project, actor=caller.actor)
        except DataplaneError as exc:
            raise _bad_request(exc) from None
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"source {name!r} not found")
        return {"deleted": name}

    # `test` and `preview` open outbound connections with the source's stored credentials (and
    # preview returns the rows), so they need the write scope — the same tier the CLI Console
    # gives `exa dataplane test/preview`.
    @app.post("/sources/{name}/test")
    def test_source(
        name: str,
        request: Request,
        project: str = "",
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, Any]:
        from examlops.connections import ConnectionError as NamedConnectionError

        authorize_source(caller, request, "write", project, name)
        _existing(name, project)
        try:
            probe = dataplane.probe_source(name, project=project)
        except (DataplaneError, NamedConnectionError) as exc:  # e.g. the connection is gone
            raise _bad_request(exc) from None
        return {"ok": probe.ok, "detail": redact(probe.detail)}

    @app.post("/sources/{name}/preview")
    def preview(
        name: str,
        request: Request,
        project: str = "",
        limit: int = Query(20, ge=1, le=1000),
        caller: Caller = Depends(authenticate_write),
    ) -> list[dict[str, Any]]:
        from examlops.connections import ConnectionError as NamedConnectionError

        authorize_source(caller, request, "write", project, name)
        _existing(name, project)
        try:
            rows = dataplane.preview(name, project=project, limit=limit)
        except (DataplaneError, NamedConnectionError) as exc:
            raise _bad_request(exc) from None
        # Rows carry whatever the source holds (timestamps, decimals, bytes): make them JSON.
        result: list[dict[str, Any]] = json.loads(json.dumps(rows, default=str))
        return result

    @app.post("/sources/{name}/pull", status_code=status.HTTP_202_ACCEPTED)
    def pull(
        name: str,
        request: Request,
        body: PullBody | None = None,
        caller: Caller = Depends(authenticate_write),
    ) -> dict[str, str]:
        body = body or PullBody()
        authorize_source(caller, request, "write", body.project, name)
        src = _existing(name, body.project)
        if not src.enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, f"source {src.key} is disabled")
        try:
            pull_id = scheduler.submit(
                name, body.project, trigger_kind="api", full=body.full, actor=_actor(caller)
            )
        except SpecError as exc:  # removed between the check and the reservation
            raise HTTPException(status.HTTP_404_NOT_FOUND, redact(str(exc))) from None
        if pull_id is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a pull of {src.key} is already queued or running"
            )
        return {"pull_id": pull_id}

    @app.get("/pulls/{pull_id}")
    def get_pull(
        pull_id: str, request: Request, caller: Caller | None = Depends(authenticate_read)
    ) -> dict[str, Any]:
        # accepted but not started, or failed before it; else it may have started between the two
        found = (
            catalog.get_pull(pull_id) or scheduler.status_of(pull_id) or catalog.get_pull(pull_id)
        )
        # Another project's pull is reported exactly like an unknown one (404), so the route does
        # not reveal that it exists.
        if found is None or not may_access_project(
            caller, "read", found.get("project") or "", audit=False
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"pull {pull_id!r} not found")
        authorize_source(caller, request, "read", found.get("project") or "", found.get("source"))
        return dict(found)

    @app.get("/sources/{name}/snapshots")
    def snapshots(
        name: str,
        request: Request,
        project: str = "",
        limit: int = Query(200, ge=1, le=1000),
        caller: Caller | None = Depends(authenticate_read),
    ) -> list[dict[str, Any]]:
        authorize_source(caller, request, "read", project, name)
        _existing(name, project)
        return [
            r
            for r in catalog.list_pulls(project=project, source=name, limit=limit)
            if r["status"] in _COMMITTED and r.get("revision")
        ]

    return app


__all__ = ["configure_logging", "create_app"]
