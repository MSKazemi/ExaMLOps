"""ADR 0130 — dataplane service: auth, pull lifecycle, scheduler, metrics."""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import dataplane  # noqa: E402
from examlops import iam as iam_mod  # noqa: E402
from examlops.data import dataplane as catalog  # noqa: E402
from examlops.data import init_db  # noqa: E402
from examlops.dataplane.connectors import registry  # noqa: E402
from examlops.dataplane.connectors.base import BaseConnector  # noqa: E402
from examlops.dataplane.service.app import create_app  # noqa: E402
from examlops.dataplane.service.scheduler import Scheduler  # noqa: E402
from examlops.dataplane.types import Probe, TableBatch  # noqa: E402

TOKEN = "a-real-dataplane-token-0123456789"


class _One(BaseConnector):
    kind = "one"
    connection_required = False

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        yield TableBatch("t", pa.RecordBatch.from_pylist([{"x": 1}]))


class _Blocking(BaseConnector):
    """Holds a pull open until the test releases it, so the pull lock is observably held."""

    kind = "blocking"
    connection_required = False

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def probe(self, conn, spec=None):
        return Probe(True, "ok")

    def read(self, conn, spec, since, limits):
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("test never released the blocking connector")
        yield TableBatch("t", pa.RecordBatch.from_pylist([{"x": 1}]))


class _Missing(BaseConnector):
    """Registered but its dependencies are 'not installed' — a pull fails before it starts."""

    kind = "missing"
    connection_required = False

    def available(self):
        return False, "missing fakelib — pip install 'examlops[dataplane-fake]'"

    def probe(self, conn, spec=None):
        return Probe(False, "unavailable")

    def read(self, conn, spec, since, limits):
        raise AssertionError("an unavailable connector must never be read")


@pytest.fixture(autouse=True)
def _iam_off(monkeypatch):
    """No developer trust file or legacy OIDC env reaches these tests; federation is opt-in."""
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=False))


@pytest.fixture
def client(tmp_path, monkeypatch):
    init_db()
    registry.reset()
    registry.register(_One())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.setenv("DATAPLANE_TOKEN", TOKEN)
    dataplane.define_source("s", "one", spec={})
    with TestClient(create_app(start_scheduler=False)) as c:
        yield c
    registry.reset()


H = {"Authorization": f"Bearer {TOKEN}"}


def _wait_for_pull(c, pid, headers=H):
    status = None
    for _ in range(100):
        status = c.get(f"/pulls/{pid}", headers=headers).json()["status"]
        if status in ("succeeded", "unchanged", "failed"):
            break
        time.sleep(0.05)
    return status


def test_health_is_open_and_other_routes_need_the_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/sources").status_code == 401
    assert client.get("/sources", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/sources", headers=H).json()[0]["name"] == "s"


def test_writes_refuse_a_placeholder_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPLANE_TOKEN", "changeme")
    with TestClient(create_app(start_scheduler=False)) as c:
        assert (
            c.post("/sources/s/pull", headers={"Authorization": "Bearer changeme"}).status_code
            == 503
        )


def test_pull_is_accepted_and_completes(client):
    r = client.post("/sources/s/pull", headers=H, json={})
    assert r.status_code == 202
    pid = r.json()["pull_id"]
    for _ in range(50):
        status = client.get(f"/pulls/{pid}", headers=H).json()["status"]
        if status in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert status == "succeeded"
    assert "dataplane_pull_total" in client.get("/metrics").text


def test_scheduler_finds_due_sources(client):
    # `client` registers the `one` connector; with bare `tmp_path` this depended on test order.
    dataplane.define_source("due", "one", spec={}, schedule="1h")
    sched = Scheduler(interval_s=3600, workers=1)
    assert [s.name for s in sched.due_sources(time.time())] == ["due"]


# ── auth details ─────────────────────────────────────────────────────────────


def test_without_any_auth_reads_are_open_and_writes_are_disabled(tmp_path, monkeypatch):
    """Loopback-only deployments: no token, no federation ⇒ reads open, writes 503."""
    init_db()
    monkeypatch.delenv("DATAPLANE_TOKEN", raising=False)
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.get("/sources").status_code == 200
        r = c.put("/sources/x", json={"connector": "files"})
        assert r.status_code == 503
        assert "DATAPLANE_TOKEN" in r.json()["detail"]


def test_a_malformed_authorization_header_is_401_and_never_echoed(client, caplog):
    caplog.set_level(logging.DEBUG)
    assert client.get("/sources", headers={"Authorization": TOKEN}).status_code == 401
    wrong = "Bearer not-the-token-but-a-secret-looking-thing"
    r = client.get("/sources", headers={"Authorization": wrong})
    assert r.status_code == 403
    assert "not-the-token" not in r.text and TOKEN not in r.text
    assert "not-the-token" not in caplog.text and TOKEN not in caplog.text


def test_source_lifecycle_over_http(client):
    body = {"connector": "one", "spec": {}, "schedule": "6h", "limits": {"max_rows": 10}}
    r = client.put("/sources/t", headers=H, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["schedule"] == "6h" and r.json()["limits"] == {"max_rows": 10}
    assert client.get("/sources/t", headers=H).json()["connector"] == "one"
    assert client.post("/sources/t/test", headers=H).json() == {"ok": True, "detail": "ok"}
    assert client.post("/sources/t/preview?limit=5", headers=H).json() == [{"x": 1}]
    assert client.delete("/sources/t", headers=H).status_code == 200
    assert client.get("/sources/t", headers=H).status_code == 404
    assert client.delete("/sources/t", headers=H).status_code == 404


def test_a_credential_in_the_spec_is_refused_without_echoing_it(client):
    r = client.put(
        "/sources/t", headers=H, json={"connector": "one", "spec": {"password": "hunter2-value"}}
    )
    assert r.status_code == 400
    assert "password" in r.json()["detail"] and "hunter2-value" not in r.text


def test_put_rejects_an_unknown_connector_and_unknown_fields(client):
    assert client.put("/sources/t", headers=H, json={"connector": "nope"}).status_code == 400
    assert (
        client.put("/sources/t", headers=H, json={"connector": "one", "token": "x"}).status_code
        == 422
    )


def test_preview_and_test_open_outbound_connections_so_they_need_write(tmp_path, monkeypatch):
    """They use a source's stored credentials — the same tier the CLI Console gives them."""
    init_db()
    registry.reset()
    registry.register(_One())
    monkeypatch.delenv("DATAPLANE_TOKEN", raising=False)
    dataplane.define_source("s", "one", spec={})
    try:
        with TestClient(create_app(start_scheduler=False)) as c:
            assert c.get("/sources/s").status_code == 200
            assert c.post("/sources/s/test").status_code == 503
            assert c.post("/sources/s/preview").status_code == 503
    finally:
        registry.reset()


# ── pull lifecycle ───────────────────────────────────────────────────────────


def test_a_pull_already_holding_the_lock_is_409(client):
    """The CLI's in-process `run_pull` and the service share one lock per source."""
    blocking = _Blocking()
    registry.register(blocking)
    dataplane.define_source("b", "blocking", spec={})
    worker = threading.Thread(target=dataplane.run_pull, args=("b",), daemon=True)
    worker.start()
    try:
        assert blocking.entered.wait(10)
        r = client.post("/sources/b/pull", headers=H, json={})
        assert r.status_code == 409, r.text
    finally:
        blocking.release.set()
        worker.join(10)
    r = client.post("/sources/b/pull", headers=H, json={})
    assert r.status_code == 202
    assert _wait_for_pull(client, r.json()["pull_id"]) in ("succeeded", "unchanged")


def test_a_queued_pull_blocks_a_second_request(client):
    """A second request while the first is still queued or running must not start a twin."""
    blocking = _Blocking()
    registry.register(blocking)
    dataplane.define_source("b", "blocking", spec={})
    first = client.post("/sources/b/pull", headers=H, json={})
    assert first.status_code == 202
    try:
        assert client.post("/sources/b/pull", headers=H, json={}).status_code == 409
    finally:
        blocking.release.set()
    assert _wait_for_pull(client, first.json()["pull_id"]) == "succeeded"


def test_pull_of_an_unknown_source_is_404_and_an_unknown_pull_id_is_404(client):
    assert client.post("/sources/nope/pull", headers=H, json={}).status_code == 404
    assert client.get("/pulls/0000000000000000abcdef", headers=H).status_code == 404


def test_a_pull_that_fails_before_it_starts_reports_failed(client):
    registry.register(_Missing())
    dataplane.define_source("m", "missing", spec={})
    r = client.post("/sources/m/pull", headers=H, json={})
    assert r.status_code == 202
    pid = r.json()["pull_id"]
    assert _wait_for_pull(client, pid) == "failed"
    row = client.get(f"/pulls/{pid}", headers=H).json()
    assert "unavailable" in row["error"]


def test_the_remote_cli_body_is_honoured(client, monkeypatch):
    """`exa dataplane pull --remote` posts {"project", "full"}."""
    from examlops.dataplane import pull as pull_mod

    seen: list[dict] = []
    real = pull_mod.run_pull

    def spy(name, **kwargs):
        seen.append(kwargs)
        return real(name, **kwargs)

    monkeypatch.setattr(pull_mod, "run_pull", spy)
    r = client.post("/sources/s/pull", headers=H, json={"project": "", "full": True})
    assert r.status_code == 202
    assert _wait_for_pull(client, r.json()["pull_id"]) == "succeeded"
    row = catalog.get_pull(r.json()["pull_id"])
    assert row["trigger_kind"] == "api"
    assert seen and seen[0]["full"] is True and seen[0]["project"] == ""
    assert seen[0]["actor"] == "dataplane-api"


def test_snapshots_list_committed_pulls_only(client):
    r = client.post("/sources/s/pull", headers=H, json={})
    assert _wait_for_pull(client, r.json()["pull_id"]) == "succeeded"
    snaps = client.get("/sources/s/snapshots", headers=H).json()
    assert [s["id"] for s in snaps] == [r.json()["pull_id"]]
    assert snaps[0]["revision"]


# ── metrics & health ─────────────────────────────────────────────────────────


def test_metrics_expose_freshness_and_up_per_source(client):
    text = client.get("/metrics").text
    assert 'dataplane_source_up{source="_global/s"} 0.0' in text  # never pulled
    r = client.post("/sources/s/pull", headers=H, json={})
    assert _wait_for_pull(client, r.json()["pull_id"]) == "succeeded"
    text = client.get("/metrics").text
    assert 'dataplane_source_up{source="_global/s"} 1.0' in text
    line = next(
        ln for ln in text.splitlines() if ln.startswith("dataplane_source_freshness_seconds{")
    )
    assert 'source="_global/s"' in line
    assert 0.0 <= float(line.rsplit(" ", 1)[1]) < 3600


# ── fix round 1 (dataplane alert design): dataplane_catalog_up + per-source failures ────────


def _has_series(text: str, metric: str) -> bool:
    """A real data line for `metric` — not just its `# HELP`/`# TYPE` comment, which prometheus_client
    always emits for a registered family even when it carries zero samples."""
    return any(
        ln.startswith(metric + " ") or ln.startswith(metric + "{") for ln in text.splitlines()
    )


def test_metrics_with_zero_sources_show_only_the_catalog_gauge(tmp_path, monkeypatch):
    """No source registered — dataplane_catalog_up must still be published every scrape, and
    dataplane_source_up (which only ever has a series per registered source) must not appear."""
    init_db()
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    with TestClient(create_app(start_scheduler=False)) as c:
        text = c.get("/metrics").text
    assert _has_series(text, "dataplane_catalog_up")
    assert "dataplane_catalog_up 1.0" in text
    assert not _has_series(text, "dataplane_source_up")


def test_a_source_whose_catalog_read_raises_reports_down_not_absent(client, monkeypatch):
    """A per-source read failure must flip the series to 0, not make it vanish (that would make
    `DataplanePullFailing`'s plain `== 0` blind to exactly the case it exists for)."""
    from examlops.data import dataplane as catalog_mod

    r = client.post("/sources/s/pull", headers=H, json={})
    assert _wait_for_pull(client, r.json()["pull_id"]) == "succeeded"
    assert 'dataplane_source_up{source="_global/s"} 1.0' in client.get("/metrics").text

    def boom(*_a, **_k):
        raise RuntimeError("catalog read failed")

    monkeypatch.setattr(catalog_mod, "list_pulls", boom)
    text = client.get("/metrics").text
    assert 'dataplane_source_up{source="_global/s"} 0.0' in text
    # No freshness sample for a source whose last-pull state is unknown, not a stale/wrong one.
    assert not any(ln.startswith("dataplane_source_freshness_seconds{") for ln in text.splitlines())


def test_a_raising_catalog_listing_reports_catalog_down(client, monkeypatch):
    """`list_source_defs()` itself failing (catalog/datastore outage) must be visible even though
    it also means the per-source gauge has nothing to report."""

    def boom(*_a, **_k):
        raise RuntimeError("db is down")

    monkeypatch.setattr(dataplane, "list_source_defs", boom)
    text = client.get("/metrics").text
    assert "dataplane_catalog_up 0.0" in text
    assert not _has_series(text, "dataplane_source_up")


def _freshness(c, source="_global/s"):
    for ln in c.get("/metrics").text.splitlines():
        if ln.startswith(f'dataplane_source_freshness_seconds{{source="{source}"}}'):
            return float(ln.rsplit(" ", 1)[1])
    return None


def test_freshness_counts_an_unchanged_pull_as_fresh(client):
    """HELP says "last successful pull (succeeded or unchanged)" — hold the value to that."""
    from datetime import UTC, datetime, timedelta

    from examlops.platform_db import get_db

    text = client.get("/metrics").text
    assert "last successful pull (succeeded or unchanged)" in text
    first = client.post("/sources/s/pull", headers=H, json={}).json()["pull_id"]
    assert _wait_for_pull(client, first) == "succeeded"
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("UPDATE dataplane_pulls SET finished_at=? WHERE id=?", (an_hour_ago, first))
    assert _freshness(client) >= 3500  # measured from the (back-dated) first pull
    for _ in range(100):  # the worker (lineage, audit) outlives the row turning `succeeded`
        if not client.app.state.scheduler.pending():
            break
        time.sleep(0.05)
    second = client.post("/sources/s/pull", headers=H, json={}).json()["pull_id"]
    assert _wait_for_pull(client, second) == "unchanged"  # same rows: no new snapshot
    assert _freshness(client) < 60  # …yet the source is fresh as of the second pull


def test_health_reports_components_and_is_always_200(client, monkeypatch):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["db"] is True and body["store"] is True
    assert body["connectors"]["one"] is True
    assert body["auth"] == "static"
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", "nosuchscheme-xyz://nowhere")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded" and r.json()["store"] is False
    assert client.get("/ready").json() == {"status": "alive"}


def test_health_store_is_false_while_the_stores_bucket_is_missing(client, monkeypatch, tmp_path):
    """A missing *bucket* is not "no snapshots yet": the first publish would fail. The root prefix
    under an existing bucket may legitimately not exist yet (a fresh store) — that is healthy."""
    import pyarrow.fs as pafs
    from fsspec.implementations.arrow import ArrowFSWrapper

    from examlops.dataplane.store import DatasetStore

    objects = tmp_path / "object-store"  # stands in for the S3 service: one dir per bucket
    objects.mkdir()

    def s3_like_store():
        fs = ArrowFSWrapper(
            pafs.SubTreeFileSystem(str(objects), pafs.LocalFileSystem()), skip_instance_cache=True
        )
        return DatasetStore(
            fs, "examlops-data/dataplane", uri_prefix="s3://examlops-data/dataplane"
        )

    monkeypatch.setattr(dataplane, "store_from_env", s3_like_store)
    body = client.get("/health").json()
    assert body["store"] is False and body["status"] == "degraded"
    (objects / "examlops-data").mkdir()  # the bucket now exists; `dataplane/` does not, yet
    assert client.get("/health").json()["store"] is True


def test_connectors_route_matches_the_cli_json_shape(client):
    rows = client.get("/connectors", headers=H).json()
    one = next(r for r in rows if r["kind"] == "one")
    assert set(one) == {"kind", "available", "detail", "connection_kinds", "incremental", "extra"}


# ── scheduler ────────────────────────────────────────────────────────────────


def test_scheduler_skips_disabled_unscheduled_and_recently_pulled_sources(client):
    dataplane.define_source("off", "one", spec={}, schedule="1h", enabled=False)
    dataplane.define_source("due", "one", spec={}, schedule="1h")
    sched = Scheduler(interval_s=3600, workers=1)
    try:
        assert [s.name for s in sched.due_sources(time.time())] == ["due"]
        submitted = sched.tick(time.time())
        assert submitted and len(submitted) == 1
        for _ in range(100):
            if not sched.pending():  # the worker is done, not just the catalog row
                break
            time.sleep(0.05)
        row = catalog.get_pull(submitted[0])
        assert row["status"] == "succeeded" and row["trigger_kind"] == "schedule"
        assert sched.due_sources(time.time()) == []  # just pulled
        assert [s.name for s in sched.due_sources(time.time() + 3601)] == ["due"]
    finally:
        sched.stop()


def test_scheduler_does_not_queue_a_twin_while_a_pull_is_pending(client):
    blocking = _Blocking()
    registry.register(blocking)
    dataplane.define_source("slow", "blocking", spec={}, schedule="1h")
    sched = Scheduler(interval_s=3600, workers=1)
    try:
        first = sched.tick(time.time())
        assert len(first) == 1
        assert blocking.entered.wait(10)
        # A later tick, long past the interval, still must not queue a second pull.
        assert sched.tick(time.time() + 7200) == []
    finally:
        blocking.release.set()
        sched.stop()


def test_scheduler_does_not_hot_loop_on_a_source_that_fails_before_starting(client):
    registry.register(_Missing())
    dataplane.define_source("m", "missing", spec={}, schedule="1h")
    sched = Scheduler(interval_s=3600, workers=1)
    try:
        now = time.time()
        assert len(sched.tick(now)) == 1
        for _ in range(100):
            if not sched.pending():
                break
            time.sleep(0.05)
        assert sched.tick(now + 60) == []  # the failed attempt counts toward the interval
    finally:
        sched.stop()


# ── task 22a: startup reaps interrupted pulls + cleans stale stage dirs ──────


def test_startup_reaps_a_pull_stuck_running_from_a_previous_process(tmp_path, monkeypatch):
    """A row left `running` by a process that crashed/OOM'd (task 22a) must come back `failed`
    once a fresh process starts — the service lifespan calls `reap_interrupted_pulls()`."""
    init_db()
    registry.reset()
    registry.register(_One())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.setenv("DATAPLANE_TOKEN", TOKEN)
    dataplane.define_source("stuck", "one", spec={})
    pull_id = catalog.new_pull_id()
    catalog.insert_pull(
        pull_id, "", "stuck", trigger_kind="manual", actor="t", parent_revision=None
    )
    assert catalog.get_pull(pull_id)["status"] == "running"
    with TestClient(create_app(start_scheduler=False)):
        pass  # lifespan startup runs and returns before the context exits
    assert catalog.get_pull(pull_id)["status"] == "failed"
    registry.reset()


def test_scheduler_tick_also_reaps_interrupted_pulls(client):
    """Cheap per-tick reap: a stuck `running` row is failed even without a service restart."""
    pull_id = catalog.new_pull_id()
    catalog.insert_pull(pull_id, "", "s", trigger_kind="manual", actor="t", parent_revision=None)
    sched = Scheduler(interval_s=3600, workers=1)
    try:
        sched.tick(time.time() + 10_000)  # far past "s"'s (unscheduled) due-ness; only reap fires
    finally:
        sched.stop()
    assert catalog.get_pull(pull_id)["status"] == "failed"


def test_startup_removes_stale_stage_dirs_under_tmpdir(tmp_path, monkeypatch):
    import os
    import tempfile

    from examlops.dataplane.pull import _STAGE_DIR_PREFIX

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    stale = tmp_path / f"{_STAGE_DIR_PREFIX}oldpull-abc123"
    stale.mkdir()
    old = time.time() - 3600  # well before this test session's process start
    os.utime(stale, (old, old))

    init_db()
    registry.reset()
    registry.register(_One())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.setenv("DATAPLANE_TOKEN", TOKEN)
    with TestClient(create_app(start_scheduler=False)):
        pass
    assert not stale.exists()
    registry.reset()


# ── identity federation (ADR 0120) ───────────────────────────────────────────


def _principal(role):
    return iam_mod.Principal(
        provider="center", issuer="https://idp.example", subject="alice", tenant="t1", role=role
    )


@pytest.fixture
def federated(client, monkeypatch):
    """Federation on: `viewer.jwt.x` / `operator.jwt.x` / `norole.jwt.x` verify, `bad.jwt.x` does not."""
    calls: list[dict] = []
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=True))

    def verify(token, config=None, *, provider_hint=None):
        role = token.split(".", 1)[0]
        if role == "bad":
            raise iam_mod.AuthenticationError("signature verification failed")
        return _principal(None if role == "norole" else role)

    def authorize(principal, action, resource=None, context=None, **kw):
        calls.append({"action": action, "resource": resource, **kw})
        return iam_mod.Decision(True, "ok")

    monkeypatch.setattr(iam_mod, "verify_access_token", verify)
    monkeypatch.setattr(iam_mod, "authorize", authorize)
    return SimpleNamespace(client=client, calls=calls)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_federated_viewer_may_read_but_not_write(federated):
    c = federated.client
    assert c.get("/sources", headers=_bearer("viewer.jwt.x")).status_code == 200
    r = c.post("/sources/s/pull", headers=_bearer("viewer.jwt.x"), json={})
    assert r.status_code == 403
    call = federated.calls[0]
    assert call["action"] == "dataplane.read" and call["local_allowed"] is True
    # Final review I12: never the caller's own tenant dressed up as the resource's (that made the
    # PDP's tenant invariant a tautology); a source-scoped route names the real project instead.
    assert call["resource"] == {"type": "dataplane", "id": "/sources", "method": "GET"}


def test_a_federated_operator_may_write(federated):
    c = federated.client
    r = c.post("/sources/s/pull", headers=_bearer("operator.jwt.x"), json={})
    assert r.status_code == 202, r.text
    assert federated.calls[-1]["action"] == "dataplane.write"
    assert _wait_for_pull(c, r.json()["pull_id"], _bearer("operator.jwt.x")) == "succeeded"


def test_the_center_pdp_can_veto(federated, monkeypatch):
    monkeypatch.setattr(
        iam_mod, "authorize", lambda *a, **k: iam_mod.Decision(False, "center policy says no")
    )
    r = federated.client.get("/sources", headers=_bearer("operator.jwt.x"))
    assert r.status_code == 403 and "center policy says no" in r.text


def test_an_invalid_federated_token_is_401_invalid_token(federated):
    r = federated.client.get("/sources", headers=_bearer("bad.jwt.x"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert "bad.jwt.x" not in r.text


def test_a_federated_identity_without_an_examlops_role_is_403(federated):
    assert federated.client.get("/sources", headers=_bearer("norole.jwt.x")).status_code == 403


def test_the_static_token_still_works_with_federation_on(federated):
    assert federated.client.post("/sources/s/pull", headers=H, json={}).status_code == 202
    assert federated.calls == []  # a static credential never consults the center's PDP


def test_a_bad_trust_file_refuses_federated_tokens_but_not_the_static_one(client, monkeypatch):
    def broken(*a, **k):
        raise iam_mod.IamConfigError("trust file is not valid YAML")

    monkeypatch.setattr(iam_mod, "load_config", broken)
    monkeypatch.setattr(
        iam_mod, "verify_access_token", lambda *a, **k: pytest.fail("must not verify")
    )
    assert client.get("/sources", headers=_bearer("operator.jwt.x")).status_code == 403
    assert client.get("/sources", headers=H).status_code == 200


def test_federation_alone_closes_reads_and_enables_writes(tmp_path, monkeypatch):
    init_db()
    registry.reset()
    registry.register(_One())
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'store'}")
    monkeypatch.delenv("DATAPLANE_TOKEN", raising=False)
    monkeypatch.setattr(iam_mod, "load_config", lambda *a, **k: SimpleNamespace(enabled=True))
    monkeypatch.setattr(iam_mod, "verify_access_token", lambda *a, **k: _principal("operator"))
    monkeypatch.setattr(iam_mod, "authorize", lambda *a, **k: iam_mod.Decision(True, "ok"))
    dataplane.define_source("s", "one", spec={})
    try:
        with TestClient(create_app(start_scheduler=False)) as c:
            assert c.get("/sources").status_code == 401
            assert c.get("/sources", headers=_bearer("operator.jwt.x")).status_code == 200
            r = c.post("/sources/s/pull", headers=_bearer("operator.jwt.x"), json={})
            assert r.status_code == 202
            _wait_for_pull(c, r.json()["pull_id"], _bearer("operator.jwt.x"))
    finally:
        registry.reset()


def test_a_bad_trust_file_with_no_static_token_fails_closed(tmp_path, monkeypatch):
    init_db()
    monkeypatch.delenv("DATAPLANE_TOKEN", raising=False)

    def broken(*a, **k):
        raise iam_mod.IamConfigError("trust file is not valid YAML")

    monkeypatch.setattr(iam_mod, "load_config", broken)
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.get("/sources").status_code == 503
        assert c.get("/sources", headers=_bearer("operator.jwt.x")).status_code == 503


# ── fix round 1: fail-closed on a bad token, no echo on 422, bounded health ──


@pytest.mark.parametrize(
    "value", ["changeme", "short-tok", "change-me-dataplane-token", "  replace-me-now  "]
)
def test_a_set_but_unusable_token_closes_reads_too(tmp_path, monkeypatch, value):
    """Only an unset/blank token (with federation off) opens reads — never a typo'd one."""
    init_db()
    monkeypatch.setenv("DATAPLANE_TOKEN", value)
    with TestClient(create_app(start_scheduler=False)) as c:
        for r in (c.get("/sources"), c.get("/sources", headers=_bearer(value.strip()))):
            assert r.status_code == 503
            assert "placeholder or too short" in r.json()["detail"]
            assert value.strip() not in r.text
        assert c.get("/health").json()["auth"] == "token-invalid"


def test_a_blank_token_is_unset(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setenv("DATAPLANE_TOKEN", "   ")
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.get("/sources").status_code == 200
        assert c.get("/health").json()["auth"] == "open"


def test_a_token_read_from_a_file_with_a_trailing_newline_still_matches(client, monkeypatch):
    monkeypatch.setenv("DATAPLANE_TOKEN", TOKEN + "\n")
    assert client.get("/sources", headers=H).status_code == 200


@pytest.mark.parametrize(
    "path,body",
    [
        ("/sources/t", {"connector": "one", "token": "SUPERSECRET-VALUE-123"}),
        ("/sources/t", {"connector": "one", "spec": "SUPERSECRET-VALUE-123"}),
        ("/sources/s/pull", {"project": "", "password": "SUPERSECRET-VALUE-123"}),
    ],
)
def test_a_rejected_body_is_never_echoed(client, path, body):
    method = client.put if path == "/sources/t" else client.post
    r = method(path, headers=H, json=body)
    assert r.status_code == 422
    assert "SUPERSECRET-VALUE-123" not in r.text
    for err in r.json()["detail"]:
        assert set(err) == {"loc", "type", "msg"}


def test_test_and_preview_of_a_source_whose_connection_is_gone_are_400(client):
    # Written straight to the catalog: define_source would refuse the unknown connection.
    catalog.upsert_source(
        "",
        "ghost",
        connector="one",
        connection="no-such-connection",
        spec={},
        schedule=None,
        limits={},
        contract=None,
        enabled=True,
        actor="t",
    )
    for route in ("/sources/ghost/test", "/sources/ghost/preview"):
        r = client.post(route, headers=H)
        assert r.status_code == 400, (route, r.text)
        assert "no-such-connection" in r.json()["detail"]


def test_health_does_not_hang_on_an_unreachable_store(client, monkeypatch):
    from examlops.dataplane.service import app as app_mod

    release = threading.Event()

    def hanging_store():
        release.wait(10)
        raise OSError("store never answered")

    monkeypatch.setattr(app_mod, "_STORE_PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(dataplane, "store_from_env", hanging_store)
    try:
        started = time.monotonic()
        first = client.get("/health").json()
        second = client.get("/health").json()  # waits on the same probe; does not start another
        assert time.monotonic() - started < 3
        assert first["store"] is False and first["status"] == "degraded"
        assert second["store"] is False
        probes = [t for t in threading.enumerate() if t.name == "dataplane-store-probe"]
        assert len(probes) <= 1
    finally:
        release.set()


def test_every_route_but_the_open_ones_is_authenticated(client):
    """A route added without an auth dependency is an unauthenticated route — and a source-scoped
    route (final review I12) that authenticates but never calls `authorize_source` is one any
    caller with the scope can use on any project."""
    import inspect

    from fastapi.routing import APIRoute

    from examlops.dataplane.service.auth import (
        authenticate_ingest,
        authenticate_read,
        authenticate_write,
        require_read,
        require_write,
    )

    open_routes = {"/health", "/ready", "/metrics"}
    docs_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    # the route itself must authorise the project (ADR 0131: push is `ingest`, never open)
    scoped = {authenticate_read, authenticate_write, authenticate_ingest}
    writes = {require_write, authenticate_write, authenticate_ingest}  # a write or an ingest

    def calls(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from calls(dep)

    checked = 0
    for route in client.app.routes:
        if not isinstance(route, APIRoute):
            assert route.path in docs_routes, f"unexpected non-API route {route.path}"
            continue
        if route.path in open_routes:
            continue
        where = f"{sorted(route.methods)} {route.path}"
        guards = set(calls(route.dependant)) & ({require_read, require_write} | scoped)
        assert guards, f"{where} has no auth dependency"
        if set(route.methods) - {"GET", "HEAD"}:  # anything that can change state, or reach out
            assert guards & writes, f"{where} is not write"
        if route.path.startswith(("/sources/{name}", "/pulls/", "/streams/{name}")):
            assert guards & scoped, f"{where} is source-scoped but not project-authorised"
        if guards & scoped:
            # a stream route authorises with `authorize_stream` (the push route hands it to the
            # threadpool, `run_in_threadpool(authorize_stream, …)`), a source route with
            # `authorize_source(`
            needle = (
                "authorize_stream" if route.path.startswith("/streams") else "authorize_source("
            )
            assert needle in inspect.getsource(route.endpoint), (
                f"{where} authenticates without authorising the project"
            )
        checked += 1
    # the guard is inspecting the real route table: 10 source/pull routes + 3 stream routes (A8)
    # + 5 runtime-control/dead-letter stream routes (A8b: state, dead-letters list/get/purge/replay)
    assert checked == 18


def test_the_center_pdp_can_veto_a_write(federated, monkeypatch):
    monkeypatch.setattr(
        iam_mod, "authorize", lambda *a, **k: iam_mod.Decision(False, "no writes on friday")
    )
    r = federated.client.post("/sources/s/pull", headers=_bearer("operator.jwt.x"), json={})
    assert r.status_code == 403 and "no writes on friday" in r.text
    r = federated.client.put(
        "/sources/t", headers=_bearer("operator.jwt.x"), json={"connector": "one"}
    )
    assert r.status_code == 403
    assert catalog.list_pulls(project="", source="s") == []
    assert catalog.get_source("t", "") is None


# ── fix round 2: no URL-bearing INFO logs, the token-invalid+federated state ──


def test_the_service_logging_config_silences_url_logging_http_clients():
    """httpx logs every request at INFO with its full URL (userinfo, signed query string)."""
    import ast
    import logging

    import httpx

    from examlops.dataplane.service.app import _URL_LOGGING_LIBRARIES, configure_logging

    secret_url = "https://svcuser:S3CRET-PW@api.example/v1/items?sig=SIGVALUE"
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    names = ("", "examlops", "examlops.dataplane.service.app", *_URL_LOGGING_LIBRARIES)
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg, lg.level, lg.disabled, lg.propagate) for lg in loggers]
    saved_disable = logging.root.manager.disable
    root = logging.getLogger()
    capture = _Capture(level=logging.DEBUG)
    root.addHandler(capture)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        # Precondition — without the config, the URL really is logged (so this test can fail).
        # Other tests in this process may have quietened these loggers; start from defaults.
        logging.disable(logging.NOTSET)
        for lg in loggers:
            lg.setLevel(logging.NOTSET)
            lg.disabled = False
            lg.propagate = True
        root.setLevel(logging.DEBUG)
        client.get(secret_url)
        assert any("SIGVALUE" in r.getMessage() for r in records), "httpx no longer logs URLs?"
        records.clear()

        configure_logging()
        client.get(secret_url)
        logging.getLogger("examlops.dataplane.service.app").info("dataplane: auth mode static")
        messages = [r.getMessage() for r in records]
        assert not any("S3CRET-PW" in m or "SIGVALUE" in m for m in messages), messages
        assert "dataplane: auth mode static" in messages  # our own INFO line still appears
        for name in ("httpx", "httpcore", "urllib3"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    finally:
        client.close()
        root.removeHandler(capture)
        for lg, level, disabled, propagate in saved:
            lg.setLevel(level)
            lg.disabled = disabled
            lg.propagate = propagate
        logging.disable(saved_disable)

    # …and the container entrypoint really applies that config before serving.
    main_py = Path(__file__).parents[2] / "platform" / "services" / "dataplane" / "main.py"
    tree = ast.parse(main_py.read_text())
    guard = next(n for n in tree.body if isinstance(n, ast.If))
    called = [
        c.func.id
        for c in ast.walk(guard)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    ]
    assert "configure_logging" in called and called.index("configure_logging") < called.index(
        "create_app"
    )
    assert "basicConfig" not in main_py.read_text()


def test_an_unusable_static_token_is_ignored_when_federation_works(federated, monkeypatch):
    """The approved ruling: a bad static token does not close a service whose IdP path works."""
    monkeypatch.setenv("DATAPLANE_TOKEN", "changeme")
    c = federated.client
    r = c.get("/sources", headers=_bearer("changeme"))
    assert r.status_code == 403 and "changeme" not in r.text
    assert c.get("/sources").status_code == 401
    assert c.get("/sources", headers=_bearer("viewer.jwt.x")).status_code == 200
    assert c.get("/health").json()["auth"] == "token-invalid+federated"


# ── final review I10: `DataplaneSourceStale` compares freshness against 2 × the schedule ─────


def _series(text: str, metric: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for ln in text.splitlines():
        if ln.startswith(metric + "{"):
            source = ln.split('source="', 1)[1].split('"', 1)[0]
            out[source] = float(ln.rsplit(" ", 1)[1])
    return out


def test_metrics_expose_the_schedule_of_scheduled_sources_only(client):
    """Only an enabled source with a schedule gets a series: an unscheduled or disabled source is
    never pulled on its own, so "stale" is meaningless for it, and the alert's `on(source)` join
    then has nothing to match."""
    dataplane.define_source("hourly", "one", spec={}, schedule="1h")
    dataplane.define_source("daily", "one", spec={}, schedule="@daily")
    dataplane.define_source("paused", "one", spec={}, schedule="15m", enabled=False)
    # A stored schedule that no longer parses is skipped, never a failed scrape.
    catalog.upsert_source(
        "",
        "garbled",
        connector="one",
        connection=None,
        spec={},
        schedule="every tuesday",
        limits={},
        contract=None,
        enabled=True,
        actor="t",
    )
    text = client.get("/metrics").text
    assert _series(text, "dataplane_source_schedule_seconds") == {
        "_global/hourly": 3600.0,
        "_global/daily": 86400.0,
    }
    assert "_global/garbled" in _series(text, "dataplane_source_up")


# ── final review I12: project-scoped authorisation for federated callers (spec §10) ─────────

OP = {"Authorization": "Bearer operator.jwt.x"}
VIEW = {"Authorization": "Bearer viewer.jwt.x"}


@pytest.fixture
def tenancy(federated, monkeypatch):
    """Multitenancy on; a source `s` in projects `a` and `b` beside the global `s`. The federated
    caller (`center:alice`) holds no relation until a test grants one."""
    from examlops import authz

    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    dataplane.define_source("s", "one", spec={}, project="a")
    dataplane.define_source("s", "one", spec={}, project="b")
    federated.grant = lambda relation, project: authz.grant(
        "center:alice", relation, f"project:{project}"
    )
    return federated


def test_a_project_editor_may_pull_its_project_but_not_another(tenancy):
    tenancy.grant("editor", "a")
    c = tenancy.client
    r = c.post("/sources/s/pull", headers=OP, json={"project": "a"})
    assert r.status_code == 202, r.text
    assert _wait_for_pull(c, r.json()["pull_id"], OP) == "succeeded"
    write = next(x for x in tenancy.calls if x["action"] == "dataplane.write")
    assert write["resource"] == {"type": "dataplane", "id": "a/s", "method": "POST", "project": "a"}

    denied = c.post("/sources/s/pull", headers=OP, json={"project": "b"})
    missing = c.post("/sources/nope/pull", headers=OP, json={"project": "b"})
    assert denied.status_code == missing.status_code == 403
    # One fixed answer: nothing about project b — not even whether a source exists there.
    assert denied.json() == missing.json()
    assert "b/s" not in denied.text and "project:b" not in denied.text


def test_a_project_viewer_may_read_but_not_write(tenancy):
    tenancy.grant("viewer", "a")
    c = tenancy.client
    assert c.get("/sources/s", headers=OP, params={"project": "a"}).status_code == 200
    assert c.get("/sources/s/snapshots", headers=OP, params={"project": "a"}).status_code == 200
    assert c.get("/sources/s", headers=OP, params={"project": "b"}).status_code == 403
    assert c.get("/sources/s/snapshots", headers=OP, params={"project": "b"}).status_code == 403
    body = {"connector": "one", "project": "a", "spec": {}}
    for r in (
        c.post("/sources/s/pull", headers=OP, json={"project": "a"}),
        c.put("/sources/s", headers=OP, json=body),
        c.delete("/sources/s", headers=OP, params={"project": "a"}),
        c.post("/sources/s/test", headers=OP, params={"project": "a"}),
        c.post("/sources/s/preview", headers=OP, params={"project": "a"}),
    ):
        assert r.status_code == 403, r.text
    assert dataplane.get_source_def("s", "a").project == "a"  # nothing was deleted


def test_listing_shows_only_the_projects_a_caller_can_view(tenancy):
    tenancy.grant("viewer", "a")
    c = tenancy.client
    listed = {(s["project"], s["name"]) for s in c.get("/sources", headers=OP).json()}
    assert listed == {("", "s"), ("a", "s")}
    assert c.get("/sources", headers=OP, params={"project": "b"}).status_code == 403
    assert len(c.get("/sources", headers=H).json()) == 3  # the platform credential sees all


def test_a_pull_of_another_project_cannot_be_read_by_id(tenancy):
    tenancy.grant("viewer", "a")
    c = tenancy.client
    pid = c.post("/sources/s/pull", headers=H, json={"project": "b"}).json()["pull_id"]
    assert _wait_for_pull(c, pid) == "succeeded"
    # Reported exactly like an unknown pull: the route does not reveal that it exists.
    foreign = c.get(f"/pulls/{pid}", headers=OP)
    unknown = c.get("/pulls/18d0000000000000000000", headers=OP)
    assert foreign.status_code == unknown.status_code == 404
    assert c.get(f"/pulls/{pid}", headers=H).status_code == 200


def test_listing_hidden_sources_writes_no_deny_audit_events(tenancy):
    """A listing filters; it is not an access attempt on every hidden project (final review FC)."""
    from examlops.data import audit as audit_data

    for i in range(20):
        dataplane.define_source(f"h{i}", "one", spec={}, project="b")
    tenancy.grant("viewer", "a")
    c = tenancy.client

    before = len(audit_data.export_audit_events())
    listed = c.get("/sources", headers=OP).json()
    assert {s["project"] for s in listed} == {"", "a"}
    assert len(audit_data.export_audit_events()) == before  # a read writes no audit events


def test_global_sources_are_read_by_any_role_and_written_by_operators(tenancy):
    c = tenancy.client  # no relation on any project
    assert c.get("/sources/s", headers=VIEW).status_code == 200
    assert c.post("/sources/s/pull", headers=VIEW, json={}).status_code == 403
    assert c.post("/sources/s/pull", headers=OP, json={}).status_code == 202


def test_a_project_role_carried_in_the_token_counts_like_a_relation(tenancy, monkeypatch):
    def as_role(project_role):
        principal = iam_mod.Principal(
            provider="center",
            issuer="https://idp.example",
            subject="alice",
            tenant="t1",
            role="operator",
            projects={"a": project_role},
        )
        monkeypatch.setattr(iam_mod, "verify_access_token", lambda *a, **k: principal)

    c = tenancy.client
    as_role("viewer")
    assert c.get("/sources/s", headers=OP, params={"project": "a"}).status_code == 200
    assert c.post("/sources/s/pull", headers=OP, json={"project": "a"}).status_code == 403
    as_role("operator")
    assert c.post("/sources/s/pull", headers=OP, json={"project": "a"}).status_code == 202
    assert c.post("/sources/s/pull", headers=OP, json={"project": "b"}).status_code == 403


def test_multitenancy_off_leaves_federated_access_unchanged(federated, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY", raising=False)
    dataplane.define_source("s", "one", spec={}, project="b")
    c = federated.client
    assert c.get("/sources/s", headers=VIEW, params={"project": "b"}).status_code == 200
    assert len(c.get("/sources", headers=VIEW).json()) == 2
    assert c.post("/sources/s/pull", headers=OP, json={"project": "b"}).status_code == 202


def test_the_static_token_keeps_full_access_under_multitenancy(tenancy):
    c = tenancy.client
    assert c.get("/sources/s", headers=H, params={"project": "a"}).status_code == 200
    assert c.post("/sources/s/pull", headers=H, json={"project": "b"}).status_code == 202
    assert tenancy.calls == []  # a static credential consults neither relations nor the PDP


def test_a_slashed_project_never_inherits_its_parents_grant(tenancy):
    """`authz.check` walks `/`-separated parents; `a/x` is no project name and must not ride on a
    grant for `a`."""
    tenancy.grant("editor", "a")
    c = tenancy.client
    body = {"connector": "one", "project": "a/x", "spec": {}}
    assert c.put("/sources/t", headers=OP, json=body).status_code == 403
    assert c.post("/sources/s/pull", headers=OP, json={"project": "a/x"}).status_code == 403
    assert c.get("/sources", headers=OP, params={"project": "a/x"}).status_code == 403
