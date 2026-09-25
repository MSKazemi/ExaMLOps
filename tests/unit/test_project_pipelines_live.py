"""ADR 0092 decision 1 — live hydration of a project's Prefect + Ray Serve pipeline surfaces.

The fakes are ``httpx.MockTransport`` handlers that answer the *exact* request shapes a Prefect
3 server and the serve app accept (the filter bodies were checked against a running Prefect
server when this was written): a deployments filter on ``tags.all_``, a flow-runs filter on
``deployment_id.any_`` + started state types sorted ``START_TIME_DESC``, and ``GET /models``
returning ``ModelInfo`` rows. Each test asserts the assembled surface, not the argv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import project_pipelines as pp  # noqa: E402

PREFECT = "http://prefect.test/api"
SERVE = "http://serve.test"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in ("PREFECT_API_URL", "RAY_SERVE_URL", "EXAMLOPS_PROJECT_PIPELINES_LIVE"):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    platform_db.create_project("climate", storage_gb=100.0)
    platform_db.assign_resource_to_project("climate", "model", "JPCP")
    platform_db.assign_resource_to_project("climate", "model", "FDATA")
    platform_db.ensure_project_storage("climate")
    yield platform_db


def _deployment(dep_id, name, *, tags, cron=None, pool="hpc", paused=False):
    return {
        "id": dep_id,
        "name": name,
        "tags": tags,
        "paused": paused,
        "work_pool_name": pool,
        "status": "READY",
        "schedules": (
            [{"active": True, "schedule": {"cron": cron, "timezone": "UTC"}}] if cron else []
        ),
    }


def _prefect_handler(deployments, runs, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path, body))
        if request.url.path == "/api/deployments/filter":
            want = set(body["deployments"]["tags"]["all_"])
            hits = [d for d in deployments if want <= set(d["tags"])]
            off, lim = body.get("offset", 0), body.get("limit", 200)
            return httpx.Response(200, json=hits[off : off + lim])
        if request.url.path == "/api/flow_runs/filter":
            ids = set(body["flow_runs"]["deployment_id"]["any_"])
            states = set(body["flow_runs"]["state"]["type"]["any_"])
            hits = [r for r in runs if r["deployment_id"] in ids and r["state_type"] in states]
            null = (body["flow_runs"].get("start_time") or {}).get("is_null_")
            if null is not None:
                hits = [r for r in hits if (r.get("start_time") is None) == null]
            # Prefect 3 sorts START_TIME_DESC on coalesce(start_time, expected_start_time)
            # (prefect/server/schemas/sorting.py), so a never-started run sorts by its schedule.
            hits.sort(
                key=lambda r: r.get("start_time") or r.get("expected_start_time") or "",
                reverse=True,
            )
            return httpx.Response(200, json=hits[: body.get("limit", 200)])
        return httpx.Response(404, json={"detail": "not found"})

    return handler


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


SRC = pp.LiveSources(prefect_api_url=PREFECT, serve_url=SERVE)


# ── Prefect surface ──────────────────────────────────────────────────────────
def test_prefect_surface_from_tagged_deployments():
    seen: list = []
    deps = [
        _deployment(
            "d1", "examlops-jpcp-nightly", tags=["examlops", "project:climate"], cron="0 2 * * *"
        ),
        _deployment(
            "d2", "examlops-fdata-nightly", tags=["examlops", "project:climate"], pool="gpu"
        ),
        _deployment("d3", "other-team", tags=["examlops", "project:other"], cron="5 5 * * *"),
    ]
    runs = [
        {"deployment_id": "d1", "state_type": "COMPLETED", "start_time": "2026-09-24T02:00:00Z"},
        {"deployment_id": "d2", "state_type": "COMPLETED", "start_time": "2026-09-24T03:00:00Z"},
        {"deployment_id": "d3", "state_type": "FAILED", "start_time": "2026-09-24T09:00:00Z"},
        {"deployment_id": "d1", "state_type": "SCHEDULED", "start_time": "2026-09-25T02:00:00Z"},
    ]
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, runs, seen))
    )
    assert surface["deployments"] == ["examlops-jpcp-nightly", "examlops-fdata-nightly"]
    assert surface["schedule"] == "0 2 * * *"
    assert surface["work_pool"] == "gpu, hpc"
    # the SCHEDULED future run and the other project's FAILED run are not "the last run"
    assert surface["last_run_state"] == "COMPLETED"
    assert surface["last_run_at"] == "2026-09-24T03:00:00Z"
    assert surface["last_run_deployment"] == "examlops-fdata-nightly"
    assert surface["status"] == "healthy"
    assert surface["ref"] == "project:climate"
    assert seen[0][1]["deployments"]["tags"]["all_"] == ["project:climate"]


def test_prefect_surface_failed_last_run_is_degraded():
    deps = [_deployment("d1", "a", tags=["project:climate"])]
    runs = [{"deployment_id": "d1", "state_type": "CRASHED", "start_time": "2026-09-24T02:00:00Z"}]
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, runs, []))
    )
    assert surface["status"] == "degraded"


def test_prefect_surface_all_paused_is_degraded():
    deps = [_deployment("d1", "a", tags=["project:climate"], paused=True)]
    runs = [
        {"deployment_id": "d1", "state_type": "COMPLETED", "start_time": "2026-09-24T02:00:00Z"}
    ]
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, runs, []))
    )
    assert surface["status"] == "degraded"


def test_prefect_surface_never_run_is_unknown_and_no_deployments_skip_run_query():
    seen: list = []
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler([], [], seen))
    )
    assert surface["deployments"] == [] and surface["status"] == "unknown"
    assert [p for p, _ in seen] == ["/api/deployments/filter"]  # no flow-run query for nothing


def test_prefect_surface_pages_and_caps(monkeypatch):
    monkeypatch.setattr(pp, "MAX_DEPLOYMENTS", 450)
    deps = [_deployment(f"d{i}", f"dep-{i:04d}", tags=["project:climate"]) for i in range(700)]
    seen: list = []
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, [], seen))
    )
    assert len(surface["deployments"]) == 450 and surface["truncated"] is True
    offsets = [b["offset"] for p, b in seen if p == "/api/deployments/filter"]
    assert offsets == [0, 200, 400]


def test_prefect_http_error_raises_for_the_caller_to_fall_back():
    def handler(request):
        return httpx.Response(503, text="down")

    with pytest.raises(pp.LiveSourceError):
        pp.fetch_prefect_surface("climate", SRC, client=_client(handler))


# ── Ray Serve surface ────────────────────────────────────────────────────────
def _serve(entries, status=200):
    def handler(request):
        assert request.url.path == "/models"
        return httpx.Response(status, json=entries)

    return _client(handler)


def test_rayserve_surface_filters_to_project_and_reports_aliases():
    entries = [
        {"model_name": "jpcp", "alias": "Production", "status": "ok", "project": None},
        {"model_name": "jpcp", "alias": "Canary", "status": "ok", "project": "climate"},
        {"model_name": "fdata", "alias": "Production", "status": "ok", "project": None},
        {"model_name": "mack", "alias": "Production", "status": "ok", "project": "climate"},
        # claimed by another project: never counted, even though its name is a member
        {"model_name": "fdata", "alias": "Staging", "status": "error", "project": "other"},
        {"model_name": "stranger", "alias": "Production", "status": "ok", "project": None},
    ]
    surface = pp.fetch_rayserve_surface("climate", ["FDATA", "JPCP"], SRC, client=_serve(entries))
    assert surface["served"] == ["FDATA", "JPCP", "mack"]
    assert surface["aliases"]["JPCP"] == ["Canary", "Production"]
    assert surface["unserved"] == []
    assert surface["status"] == "healthy"
    assert "stranger" not in surface["health"]


def test_rayserve_unserved_or_unhealthy_is_degraded():
    entries = [{"model_name": "jpcp", "alias": "Production", "status": "error", "project": None}]
    surface = pp.fetch_rayserve_surface("climate", ["FDATA", "JPCP"], SRC, client=_serve(entries))
    assert surface["health"] == {"JPCP": "error"}
    assert surface["unserved"] == ["FDATA"]
    assert surface["status"] == "degraded"


def test_rayserve_sends_serving_token_only_when_set():
    got = {}

    def handler(request):
        got["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    src = pp.LiveSources(serve_url=SERVE, serving_token="vk-123")
    pp.fetch_rayserve_surface("climate", [], src, client=_client(handler))
    assert got["auth"] == "Bearer vk-123"
    pp.fetch_rayserve_surface(
        "climate", [], pp.LiveSources(serve_url=SERVE), client=_client(handler)
    )
    assert got["auth"] is None


def test_rayserve_bad_shape_raises():
    with pytest.raises(pp.LiveSourceError):
        pp.fetch_rayserve_surface("climate", [], SRC, client=_serve({"not": "a list"}))


# ── hydrate: merge, cache, fail-open ─────────────────────────────────────────
def test_hydrate_overlays_live_and_caches_registry_row(_db):
    deps = [_deployment("d1", "examlops-jpcp-nightly", tags=["project:climate"], cron="0 2 * * *")]
    runs = [
        {"deployment_id": "d1", "state_type": "COMPLETED", "start_time": "2026-09-24T02:00:00Z"}
    ]
    base = _db.get_project_pipelines("climate", live=pp.LiveSources())
    out = pp.hydrate(
        "climate",
        ["FDATA", "JPCP"],
        base,
        SRC,
        prefect_client=_client(_prefect_handler(deps, runs, [])),
        serve_client=_serve([{"model_name": "jpcp", "alias": "Production", "status": "ok"}]),
    )
    assert out["prefect"]["source"] == "live"
    assert out["prefect"]["deployments"] == ["examlops-jpcp-nightly"]
    assert out["prefect"]["storage_prefix"] == "s3://examlops-projects/climate/"
    assert out["rayserve"]["source"] == "live" and out["rayserve"]["status"] == "degraded"
    assert out["rayserve"]["unserved"] == ["FDATA"]

    # write-through: the next registry-only read reflects the last observed state
    cached = _db.get_project_pipelines("climate", live=pp.LiveSources())
    assert cached["prefect"]["schedule"] == "0 2 * * *"
    assert cached["prefect"]["last_run_at"] == "2026-09-24T02:00:00Z"
    assert cached["prefect"]["status"] == "healthy"
    assert cached["rayserve"]["status"] == "degraded"
    with _db.get_db() as conn:
        refs = {r["kind"]: r["ref"] for r in conn.execute("SELECT * FROM project_pipelines")}
    assert refs == {"prefect": "project:climate", "rayserve": pp.SERVE_APP}


def test_hydrate_fails_open_to_registry_on_unreachable_sources(_db):
    _db.upsert_project_pipeline("climate", "prefect", "project:climate", status="healthy")

    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    base = _db.get_project_pipelines("climate", live=pp.LiveSources())
    out = pp.hydrate(
        "climate",
        ["FDATA", "JPCP"],
        base,
        SRC,
        prefect_client=_client(boom),
        serve_client=_client(boom),
    )
    assert out["prefect"]["source"] == "registry"
    assert out["prefect"]["status"] == "healthy"  # the cached row, not "unknown"
    assert "refused" in out["prefect"]["live_error"]
    assert out["rayserve"]["source"] == "registry" and "live_error" in out["rayserve"]
    # a failed live read never rewrites the cache
    with _db.get_db() as conn:
        row = conn.execute("SELECT status FROM project_pipelines WHERE kind='prefect'").fetchone()
    assert row["status"] == "healthy"


def test_get_project_pipelines_contacts_nothing_without_explicit_sources(_db, monkeypatch):
    def forbidden(*a, **k):  # pragma: no cover - the assertion is that it is never reached
        raise AssertionError("contacted a live source with none configured")

    monkeypatch.setattr(pp, "fetch_prefect_surface", forbidden)
    monkeypatch.setattr(pp, "fetch_rayserve_surface", forbidden)
    out = _db.get_project_pipelines("climate")
    assert out["prefect"]["source"] == "registry"
    assert out["prefect"]["storage_prefix"] == "s3://examlops-projects/climate/"


def test_env_sources_and_kill_switch(monkeypatch):
    monkeypatch.setenv("PREFECT_API_URL", "http://orchestrator:4200/api")
    monkeypatch.setenv("RAY_SERVE_URL", "http://ray-serving:8001/")
    src = pp.sources_from_env()
    assert src.prefect_api_url == "http://orchestrator:4200/api"
    assert src.serve_url == "http://ray-serving:8001"
    monkeypatch.setenv("EXAMLOPS_PROJECT_PIPELINES_LIVE", "off")
    assert pp.sources_from_env().enabled is False


def test_prefect_api_base_normalises_ui_root():
    assert pp.prefect_api_base("http://h:14200") == "http://h:14200/api"
    assert pp.prefect_api_base("http://h:14200/api/") == "http://h:14200/api"
    assert pp.prefect_api_base("") is None


def test_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROJECT_PIPELINES_TIMEOUT", "999")
    assert pp.sources(prefect_url="http://h").timeout == 30.0
    monkeypatch.setenv("EXAMLOPS_PROJECT_PIPELINES_TIMEOUT", "nonsense")
    assert pp.sources(prefect_url="http://h").timeout == 2.0


def test_unknown_project_live_read_creates_no_surface(_db):
    _db.create_project("empty")
    base = _db.get_project_pipelines("empty", live=pp.LiveSources())
    out = pp.hydrate(
        "empty",
        [],
        base,
        SRC,
        prefect_client=_client(_prefect_handler([], [], [])),
        serve_client=_serve([]),
    )
    assert out == {"prefect": None, "rayserve": None}
    with _db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_pipelines").fetchone()["n"] == 0


@pytest.mark.parametrize(("count", "truncated"), [(400, False), (401, True)])
def test_prefect_truncated_only_when_rows_exist_past_the_cap(monkeypatch, count, truncated):
    # A cap that lands exactly on a page boundary: 400 deployments fill it without exceeding it,
    # so the surface is complete and must not claim `truncated`; one more and it is truncated.
    monkeypatch.setattr(pp, "MAX_DEPLOYMENTS", 400)
    deps = [_deployment(f"d{i}", f"dep-{i:04d}", tags=["project:climate"]) for i in range(count)]
    seen: list = []
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, [], seen))
    )
    assert len(surface["deployments"]) == 400
    assert surface["truncated"] is truncated


def test_cancelled_never_started_run_does_not_mask_a_failure():
    # Cancelling a scheduled run leaves it CANCELLED with start_time=None and a FUTURE
    # expected_start_time. Prefect's START_TIME_DESC coalesces the two, so without a
    # start_time filter that run sorts first and hides the real (failed) last run.
    deps = [_deployment("d1", "a", tags=["project:climate"])]
    runs = [
        {"deployment_id": "d1", "state_type": "FAILED", "start_time": "2026-09-24T02:00:00Z"},
        {
            "deployment_id": "d1",
            "state_type": "CANCELLED",
            "start_time": None,
            "expected_start_time": "2026-09-30T02:00:00Z",
        },
    ]
    surface = pp.fetch_prefect_surface(
        "climate", SRC, client=_client(_prefect_handler(deps, runs, []))
    )
    assert surface["last_run_state"] == "FAILED"
    assert surface["last_run_at"] == "2026-09-24T02:00:00Z"
    assert surface["status"] == "degraded"


def test_prefect_read_is_bounded_by_a_total_budget(monkeypatch, _db):
    # Each request is individually within its timeout, but a slow server answering every page
    # at the limit must not hold the caller for 7x the timeout: the read stops at its budget and
    # the surface falls back to the registry with the reason recorded.
    now = [0.0]
    monkeypatch.setattr(pp, "_clock", lambda: now[0])
    deps = [_deployment(f"d{i}", f"dep-{i:04d}", tags=["project:climate"]) for i in range(1000)]
    seen: list = []
    inner = _prefect_handler(deps, [], seen)

    def slow(request):
        now[0] += 2.0  # every answer takes the full 2 s per-request timeout
        return inner(request)

    with pytest.raises(pp.LiveSourceError, match="budget"):
        pp.fetch_prefect_surface("climate", SRC, client=_client(slow))
    assert len(seen) == 3  # 3 x 2 s = the 6 s budget, not all 5 pages + probe + runs

    from examlops.data.projects import _registry_pipelines

    out = pp.hydrate(
        "climate",
        ["JPCP", "FDATA"],
        _registry_pipelines("climate"),
        pp.LiveSources(prefect_api_url=PREFECT),
        prefect_client=_client(slow),
    )
    assert out["prefect"]["source"] == "registry"
    assert "budget" in out["prefect"]["live_error"]
