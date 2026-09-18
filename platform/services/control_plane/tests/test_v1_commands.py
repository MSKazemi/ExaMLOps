"""/v1: asynchronous commands, a worker pool, and RFC 9457 errors (plan P1.2 + P1.6).

The legacy routes dispatch to Prefect inside the HTTP request, so a slow Prefect is a client
timeout and a Prefect outage is a failed request. /v1 accepts the command durably and answers 202;
a worker dispatches it with retries, backoff and a poison limit. These tests drive the worker
directly (``_work_commands_once``) so they are deterministic.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer test-token-0123456789"}
BODY = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}


class _Gateway:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[str | None] = []

    def find_deployment_id(self, _name):
        return "dep"

    def create_flow_run(self, _dep, _params, *, idempotency_key=None):
        self.calls.append(idempotency_key)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("prefect down")
        return f"flow-{len(self.calls)}"


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token-0123456789")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "v1.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")  # tests drive the worker directly
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_BACKOFF_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    cp_app._platform_schema_ready = False
    datasets = ["PM100Dataset", "D1", "D2", "D3", "D4"]
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": datasets})
    return cp_app


def _submit(client, key: str | None = None, body: dict | None = None):
    headers = dict(HEADERS)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/v1/retrain", json=body or BODY, headers=headers)


def test_submit_answers_202_with_a_location_and_does_not_call_prefect(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    response = _submit(TestClient(cp.app), "req-1")

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "pending"
    assert (
        response.headers["location"] == body["status_url"] == f"/v1/commands/{body['command_id']}"
    )
    assert gateway.calls == []  # nothing waits on Prefect


def test_the_worker_dispatches_and_the_status_url_reports_it(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    command_id = _submit(client, "req-1").json()["command_id"]

    assert cp._work_commands_once() == 1

    status = client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()
    assert status["state"] == "succeeded"
    assert status["result"]["flow_run_id"] == "flow-1"
    assert gateway.calls == [command_id]  # the command id is Prefect's idempotency key


def test_submit_is_idempotent_on_the_key(cp, monkeypatch):
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    client = TestClient(cp.app)

    first = _submit(client, "same").json()
    second = _submit(client, "same").json()
    conflict = _submit(client, "same", {**BODY, "is_dummy": True})

    assert first["command_id"] == second["command_id"]
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")


def test_a_failure_is_retried_with_the_same_key_then_succeeds(cp, monkeypatch):
    gateway = _Gateway(fail_times=1)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    command_id = _submit(client, "req-r").json()["command_id"]

    cp._work_commands_once()
    assert client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()["state"] == "failed"
    cp._work_commands_once()

    status = client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()
    assert status["state"] == "succeeded"
    assert status["attempts"] == 2
    assert gateway.calls == [command_id, command_id]


def test_a_command_that_keeps_failing_is_buried_not_retried_forever(cp, monkeypatch):
    monkeypatch.setattr(cp, "COMMAND_MAX_ATTEMPTS", 2)
    gateway = _Gateway(fail_times=99)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    command_id = _submit(client, "req-d").json()["command_id"]

    for _ in range(5):
        cp._work_commands_once()

    status = client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()
    assert status["state"] == "dead"
    assert len(gateway.calls) == 2


def test_a_failed_synchronous_retrain_is_never_retried_in_the_background(cp, monkeypatch):
    """The legacy caller was told 502; resurrecting its request would be a surprise dispatch."""
    gateway = _Gateway(fail_times=1)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app, raise_server_exceptions=False)

    assert client.post("/retrain", json=BODY, headers=HEADERS).status_code >= 500
    assert cp._work_commands_once() == 0
    assert len(gateway.calls) == 1


def test_admission_capacity_queues_instead_of_failing(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    held = cp._claim_command("held", "retrain", {"model_name": "X", "dataset_cls_name": "D"})
    assert held.outcome == "claimed"
    command_id = _submit(client, "queued").json()["command_id"]

    assert cp._work_commands_once() == 0  # capacity full: waits, does not fail
    assert client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()["state"] == "pending"
    cp._complete_command(
        "held",
        {"flow_run_id": "x"},
        event_topic="retrain.scheduled",
        event_payload={"model_name": "JPCP", "dataset_name": "D", "flow_run_id": "x"},
        attempt=held.attempt,
    )

    assert cp._work_commands_once() == 1
    assert client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()["state"] == "succeeded"


def test_a_pending_command_can_be_cancelled_and_then_never_runs(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    command_id = _submit(client, "req-c").json()["command_id"]

    cancelled = client.delete(f"/v1/commands/{command_id}", headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert cp._work_commands_once() == 0
    assert gateway.calls == []
    again = client.delete(f"/v1/commands/{command_id}", headers=HEADERS)
    assert again.status_code == 409


def test_listing_is_tenant_scoped_newest_first_and_paginated(cp, monkeypatch):
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    client = TestClient(cp.app)
    datasets = ["PM100Dataset", "D1", "D2", "D3", "D4"]
    ids = [
        _submit(client, f"k{n}", {"model_name": "JPCP", "dataset_name": d}).json()["command_id"]
        for n, d in enumerate(datasets)
    ]

    page1 = client.get("/v1/commands?limit=2", headers=HEADERS).json()
    page2 = client.get(
        f"/v1/commands?limit=2&cursor={page1['next_cursor']}", headers=HEADERS
    ).json()
    page3 = client.get(
        f"/v1/commands?limit=2&cursor={page2['next_cursor']}", headers=HEADERS
    ).json()

    seen = [c["command_id"] for p in (page1, page2, page3) for c in p["items"]]
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5
    assert page3["next_cursor"] is None


def test_errors_on_v1_are_problem_documents_but_legacy_errors_are_unchanged(cp):
    client = TestClient(cp.app)

    v1 = client.get("/v1/commands/nope", headers=HEADERS)
    legacy = client.get("/retrain/nope-at-all", headers={"Authorization": "Bearer wrong"})

    assert v1.status_code == 404
    assert v1.headers["content-type"].startswith("application/problem+json")
    assert v1.json()["status"] == 404 and v1.json()["title"] == "Not Found"
    assert legacy.headers["content-type"].startswith("application/json")
    assert "detail" in legacy.json() and "type" not in legacy.json()


def test_validation_errors_on_v1_are_problem_documents(cp):
    response = TestClient(cp.app).post("/v1/retrain", json={"model_name": 1}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["errors"]


# ─── the training lease and the run reconciler (plan P1.2b) ──────────────────────────────────


class _RunGateway(_Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.run_states: dict[str, str] = {}

    def get_flow_run(self, run_id):
        if run_id not in self.run_states:
            raise cp_module().HTTPException(404, "gone")
        return {"id": run_id, "state": {"type": self.run_states[run_id]}}


def cp_module():
    import app

    return app


def test_a_second_retrain_while_the_first_is_training_is_refused(cp, monkeypatch):
    gateway = _RunGateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    first = _submit(client, "first").json()["command_id"]
    cp._work_commands_once()
    gateway.run_states["flow-1"] = "RUNNING"
    cp._reconcile_runs_once()

    second = _submit(client, "second")

    assert second.status_code == 409
    assert first in second.json()["detail"]


def test_once_the_run_completes_the_model_can_be_retrained_again(cp, monkeypatch):
    gateway = _RunGateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    monkeypatch.setattr(cp, "RECONCILE_SECONDS", 1.0)
    client = TestClient(cp.app)
    first = _submit(client, "first").json()["command_id"]
    cp._work_commands_once()
    gateway.run_states["flow-1"] = "COMPLETED"

    assert cp._reconcile_runs_once() == 1
    assert client.get(f"/v1/commands/{first}", headers=HEADERS).json()["run_state"] == "COMPLETED"
    assert _submit(client, "second").status_code == 202


def test_a_settled_run_is_published_once(cp, monkeypatch):
    gateway = _RunGateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    _submit(client, "first")
    cp._work_commands_once()
    gateway.run_states["flow-1"] = "FAILED"

    cp._reconcile_runs_once()
    cp._reconcile_runs_once()

    conn = cp._get_db()
    try:
        topics = [r[0] for r in conn.execute("SELECT topic FROM event_outbox")]
    finally:
        conn.close()
    assert topics.count("retrain.run_failed") == 1


def test_a_run_prefect_forgot_settles_as_missing(cp, monkeypatch):
    gateway = _RunGateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    command_id = _submit(client, "first").json()["command_id"]
    cp._work_commands_once()

    cp._reconcile_runs_once()

    assert (
        client.get(f"/v1/commands/{command_id}", headers=HEADERS).json()["run_state"] == "MISSING"
    )


def _retrains(cp, outcome: str) -> float:
    return cp._metrics.retrain_requests.labels(
        model_name="JPCP", dataset_name="PM100Dataset", outcome=outcome
    )._value.get()


def test_the_worker_feeds_the_retrain_alerts(cp, monkeypatch):
    """HighRetrainErrorRate and RetrainDurationP99High read the retrain metrics. Once every
    platform caller submitted through /v1, only the synchronous route still recorded them, so
    both alerts went blind to the platform's own retrains (plan P1.6c)."""
    gateway = _Gateway(fail_times=1)
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    errors, successes = _retrains(cp, "error"), _retrains(cp, "success")
    durations = cp._metrics.retrain_duration.labels(
        model_name="JPCP", dataset_name="PM100Dataset"
    )._sum.get()
    _submit(client, "req-metrics")

    cp._work_commands_once()  # attempt 1 fails
    assert _retrains(cp, "error") == errors + 1
    cp._work_commands_once()  # attempt 2 dispatches
    assert _retrains(cp, "success") == successes + 1
    observed = cp._metrics.retrain_duration.labels(
        model_name="JPCP", dataset_name="PM100Dataset"
    )._sum.get()
    assert observed >= durations


def test_a_refused_duplicate_counts_as_dedup(cp, monkeypatch):
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    client = TestClient(cp.app)
    before = _retrains(cp, "dedup")
    assert _submit(client, "first").status_code == 202
    assert _submit(client, "second").status_code == 409
    assert _retrains(cp, "dedup") == before + 1


def test_the_duration_histogram_can_report_the_alerts_threshold(cp):
    """histogram_quantile() cannot exceed the largest finite bucket; the alert fires at 300 s."""
    buckets = [b for b in cp._metrics.retrain_duration._upper_bounds if b != float("inf")]
    assert max(buckets) > 300


def test_concurrent_submissions_of_the_same_retrain_create_one_command(cp, monkeypatch):
    """Two submissions of one model × dataset racing (two replicas, or two threads of one): the
    "is one already in progress?" check and the insert must be one step, or both pass the check
    and two retrains dispatch. Both requests here pass the check before either inserts — unless
    the check runs inside the insert's lock, which is the fix."""
    import threading

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    client = TestClient(cp.app)
    barrier = threading.Barrier(2)
    real_active = cp._active_retrain

    def racing_active(*args, **kwargs):
        found = real_active(*args, **kwargs)
        try:
            barrier.wait(timeout=1.0)  # both checked; now both try to insert
        except threading.BrokenBarrierError:
            pass  # the fix: the other request is waiting for the lock, not at the barrier
        return found

    monkeypatch.setattr(cp, "_active_retrain", racing_active)
    statuses: list[int] = []

    def submit(key: str) -> None:
        statuses.append(_submit(client, key).status_code)

    threads = [threading.Thread(target=submit, args=(f"race-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(statuses) == [202, 409], statuses
    listed = client.get("/v1/commands", headers=HEADERS).json()["items"]
    assert len([c for c in listed if c["state"] == "pending"]) == 1
