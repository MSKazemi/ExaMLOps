"""Durable command, admission, and outbox behavior for Prefect dispatches."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "commands.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset", "Other"]})
    return cp_app


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer test-token"}
    if key:
        headers["X-Idempotency-Key"] = key
    return headers


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def find_deployment_id(self, _name: str) -> str:
        return "deployment-1"

    def create_flow_run(self, _deployment_id, _parameters, *, idempotency_key=None) -> str:
        self.calls.append(idempotency_key)
        return "flow-1"


def test_retrain_replays_durable_result_and_commits_admission_and_outbox(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}

    first = client.post("/retrain", json=body, headers=_headers("request-7"))
    assert first.status_code == 200
    second = client.post("/retrain", json=body, headers=_headers("request-7"))

    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(gateway.calls) == 1
    assert gateway.calls[0].startswith("retrain:")

    conn = cp._get_db()
    try:
        command = conn.execute("SELECT state, attempts FROM control_plane_commands").fetchone()
        admission = conn.execute("SELECT state FROM admission_queue").fetchone()
        event = conn.execute("SELECT topic, payload FROM event_outbox").fetchone()
    finally:
        conn.close()
    assert tuple(command) == ("succeeded", 1)
    assert admission[0] == "done"
    assert event[0] == "retrain.scheduled"
    assert json.loads(event[1])["flow_run_id"] == "flow-1"


def test_idempotency_key_cannot_be_reused_for_different_input(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    client = TestClient(cp.app)

    first = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_headers("same-key"),
    )
    second = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "Other"},
        headers=_headers("same-key"),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(gateway.calls) == 1


def test_stale_approval_dispatch_is_recovered_with_same_prefect_key(cp, monkeypatch):
    conn = cp._get_db()
    try:
        conn.execute(
            "INSERT INTO pending_approvals (id, model_id, status, requested_at) "
            "VALUES ('approval-1', 'JPCP', 'approving', '2020-01-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    parameters = {
        "model_name": "JPCP",
        "dataset_cls_name": "PM100Dataset",
        "is_dummy": False,
        "backend_name": None,
    }
    assert (
        cp._claim_command(
            "approval:approval-1",
            "approval",
            parameters,
            approval_id="approval-1",
            actor="legacy",
        ).outcome
        == "claimed"
    )
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='approval:approval-1'"
        )
        conn.commit()
    finally:
        conn.close()

    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    response = TestClient(cp.app).post("/approve/JPCP", headers=_headers())

    assert response.status_code == 200
    assert gateway.calls == ["approval:approval-1"]
    conn = cp._get_db()
    try:
        approval = conn.execute(
            "SELECT status, prefect_run_id FROM pending_approvals WHERE id='approval-1'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(approval) == ("approved", "flow-1")


def test_superseded_worker_cannot_complete_newer_claim(cp):
    parameters = {"model_name": "JPCP", "dataset_cls_name": "PM100Dataset"}
    first = cp._claim_command("retrain:fenced", "retrain", parameters)
    assert first.outcome == "claimed"

    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='retrain:fenced'"
        )
        conn.commit()
    finally:
        conn.close()
    second = cp._claim_command("retrain:fenced", "retrain", parameters)
    assert second.outcome == "claimed"
    assert second.attempt == 2

    with pytest.raises(RuntimeError, match="lease was superseded"):
        cp._complete_command(
            "retrain:fenced",
            {"flow_run_id": "stale-flow"},
            event_topic="retrain.scheduled",
            event_payload={"model_name": "JPCP", "dataset_name": "D", "flow_run_id": "stale-flow"},
            attempt=first.attempt or 0,
        )

    cp._complete_command(
        "retrain:fenced",
        {"flow_run_id": "winning-flow"},
        event_topic="retrain.scheduled",
        event_payload={"model_name": "JPCP", "dataset_name": "D", "flow_run_id": "winning-flow"},
        attempt=second.attempt or 0,
    )
    conn = cp._get_db()
    try:
        command = conn.execute(
            "SELECT state, prefect_run_id FROM control_plane_commands "
            "WHERE command_key='retrain:fenced'"
        ).fetchone()
        events = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE topic='retrain.scheduled'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert tuple(command) == ("succeeded", "winning-flow")
    assert events == 1


def test_prefect_create_flow_run_sends_idempotency_key(cp, monkeypatch):
    gateway = cp.PrefectGateway("http://prefect/api")
    captured = {}

    def fake_post(url, body):
        captured.update({"url": url, "body": body})
        return {"id": "flow-9"}

    monkeypatch.setattr(gateway, "_post", fake_post)
    assert gateway.create_flow_run("dep-9", {"x": 1}, idempotency_key="command-9") == "flow-9"
    assert captured["body"] == {"parameters": {"x": 1}, "idempotency_key": "command-9"}


def test_modelzoo_retry_does_not_create_a_second_flow(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    cp._auto_retrain_model("JPCP", "PM100Dataset", "commit-1")
    cp._auto_retrain_model("JPCP", "PM100Dataset", "commit-1")

    assert gateway.calls == ["modelzoo:commit-1:JPCP:PM100Dataset"]
    conn = cp._get_db()
    try:
        freshness = conn.execute(
            "SELECT is_stale, last_retrain_commit FROM model_freshness WHERE model_id='JPCP'"
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE topic='modelzoo.retrain_scheduled'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert tuple(freshness) == (0, "commit-1")
    assert event_count == 1


# ─── Admission must never wedge a tenant (plan P0.1 / finding B1) ─────────────────────────────
#
# The HTTP caller is the worker for this synchronous API, so a request refused at capacity has
# already gone away with its answer. Its admission row used to stay ``queued`` at the head of the
# tenant's FIFO, and admission then refused every *other* key because it was not the head. Every
# internal caller uses a fresh key per request, so nothing ever retried the head: one refusal
# stopped every retrain in the tenant, permanently. Reproduced 2026-09-10 before this fix.


def _params(n: int) -> dict[str, object]:
    return {"model_name": "JPCP", "dataset_cls_name": "PM100Dataset", "n": n}


def _admission_states(cp) -> list[str]:
    conn = cp._get_db()
    try:
        return [r[0] for r in conn.execute("SELECT state FROM admission_queue ORDER BY id")]
    finally:
        conn.close()


def test_refused_admission_does_not_wedge_the_tenant(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "2")
    first = cp._claim_command("k1", "retrain", _params(1))
    second = cp._claim_command("k2", "retrain", _params(2))
    refused = cp._claim_command("k3", "retrain", _params(3))
    assert (first.outcome, second.outcome, refused.outcome) == ("claimed", "claimed", "capacity")

    for key, claim in (("k1", first), ("k2", second)):
        cp._complete_command(
            key,
            {"flow_run_id": key},
            event_topic="retrain.scheduled",
            event_payload={"model_name": "JPCP", "dataset_name": "D", "flow_run_id": key},
            attempt=claim.attempt,
        )

    # Capacity is free again: a brand-new request is admitted, not blocked behind k3's ghost row.
    assert cp._claim_command("k4", "retrain", _params(4)).outcome == "claimed"
    assert "queued" not in _admission_states(cp)


def test_refused_key_can_be_retried_after_capacity_frees(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    first = cp._claim_command("k1", "retrain", _params(1))
    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "capacity"
    cp._complete_command(
        "k1",
        {"flow_run_id": "r1"},
        event_topic="retrain.scheduled",
        event_payload={"model_name": "JPCP", "dataset_name": "D", "flow_run_id": "r1"},
        attempt=first.attempt,
    )
    # The caller that was told to retry does so with the same idempotency key.
    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "claimed"


def test_crashed_dispatch_releases_its_admission_slot(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    assert cp._claim_command("k1", "retrain", _params(1)).outcome == "claimed"
    # The worker dies mid-dispatch: the command lease goes stale and nobody completes it.
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at='2020-01-01T00:00:00' "
            "WHERE command_key='k1'"
        )
        conn.commit()
    finally:
        conn.close()

    assert cp._claim_command("k2", "retrain", _params(2)).outcome == "claimed"


def test_retrain_at_capacity_is_429_with_retry_after(cp, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "1")
    assert cp._claim_command("held", "retrain", _params(0)).outcome == "claimed"
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())

    response = TestClient(cp.app).post(
        "/retrain", json={"model_name": "JPCP", "dataset_name": "PM100Dataset"}, headers=_headers()
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After")
    assert "capacity" in response.json()["detail"]


# ─── DB access without a process-wide lock (plan P1.4 / finding P2) ─────────────────────────


def test_schema_ddl_runs_once_per_database(cp, monkeypatch):
    calls = {"n": 0}
    real = cp._apply_cp_schema

    def _counting(conn):
        calls["n"] += 1
        real(conn)

    monkeypatch.setattr(cp, "_apply_cp_schema", _counting)
    cp._cp_schema_ready.clear()
    for _ in range(5):
        cp._get_db().close()

    assert calls["n"] == 1


def test_concurrent_claims_are_each_admitted_exactly_once(cp, monkeypatch):
    """16 threads, distinct keys, generous caps: every claim succeeds, none twice, none lost."""
    import threading

    monkeypatch.setenv("EXAMLOPS_ADMISSION_MAX_RUNNING", "100")
    monkeypatch.setenv("EXAMLOPS_ADMISSION_PER_TENANT", "100")
    cp._get_db().close()  # schema first, so the race is on the claims
    outcomes: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def _claim(n: int) -> None:
        barrier.wait()
        try:
            outcomes.append(cp._claim_command(f"k{n}", "retrain", _params(n)).outcome)
        except BaseException as exc:  # noqa: BLE001 - surface any lock error to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_claim, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors, errors
    assert outcomes.count("claimed") == 16
    conn = cp._get_db()
    try:
        running = conn.execute("SELECT COUNT(*) FROM admission_queue WHERE state='running'")
        assert running.fetchone()[0] == 16
    finally:
        conn.close()


# ── A replica that dies holding a claim (plan P5 failover drill) ─────────────────────────────


_PARAMS = {
    "model_name": "JPCP",
    "dataset_cls_name": "PM100Dataset",
    "is_dummy": False,
    "backend_name": None,
}


def _abandon(cp, key: str) -> None:
    """What a crashed replica leaves: its claim, older than the lease."""
    stale = "2020-01-01T00:00:00"
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at=? WHERE command_key=?", (stale, key)
        )
        conn.commit()
    finally:
        conn.close()


def _state(cp, key: str) -> tuple:
    conn = cp._get_db()
    try:
        return tuple(
            conn.execute(
                "SELECT state, attempts FROM control_plane_commands WHERE command_key=?", (key,)
            ).fetchone()
        )
    finally:
        conn.close()


def test_a_queued_command_abandoned_mid_dispatch_is_taken_over(cp, monkeypatch):
    """The kind failover drill found these stranded in `dispatching` forever, and their model x
    dataset pair refused every later retrain with 409."""
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    submitted = TestClient(cp.app).post("/v1/retrain", json=body, headers=_headers("crash-1"))
    assert submitted.status_code == 202
    key = submitted.json()["command_id"]
    # A replica's worker claims it and dies before finishing.
    assert cp._claim_command(key, "retrain", _PARAMS, actor="legacy").outcome == "claimed"
    assert cp._work_commands_once() == 0  # within its lease, no one else touches it
    _abandon(cp, key)
    assert cp._work_commands_once() == 1
    assert _state(cp, key) == ("succeeded", 2)
    assert gateway.calls == [key]  # the command's own key: Prefect returns any run it made


def test_a_command_that_keeps_killing_its_dispatcher_is_buried(cp, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    key = (
        TestClient(cp.app)
        .post("/v1/retrain", json=body, headers=_headers("poison"))
        .json()["command_id"]
    )
    for _ in range(cp.COMMAND_MAX_ATTEMPTS):
        assert cp._claim_command(key, "retrain", _PARAMS, actor="legacy").outcome == "claimed"
        _abandon(cp, key)
    assert cp._work_commands_once() == 0
    assert _state(cp, key) == ("dead", cp.COMMAND_MAX_ATTEMPTS)
    assert gateway.calls == []  # no further attempt to crash another replica


def test_an_abandoned_synchronous_dispatch_stops_blocking_its_pair(cp, monkeypatch):
    """Synchronous commands are never retried in the background: the caller retries with its key.
    An abandoned one is marked failed, so it no longer counts as a retrain in progress."""
    assert cp._claim_command("sync-1", "retrain", _PARAMS, actor="legacy").outcome == "claimed"
    assert cp._active_retrain("default", "JPCP", "PM100Dataset") == "sync-1"
    _abandon(cp, "sync-1")
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)
    cp._work_commands_once()
    assert _state(cp, "sync-1")[0] == "failed"
    assert cp._active_retrain("default", "JPCP", "PM100Dataset") is None
    assert gateway.calls == []  # not dispatched in the background
    # The caller's retry with the same key reclaims it and dispatches with the same Prefect key.
    assert cp._claim_command("sync-1", "retrain", _PARAMS, actor="legacy").outcome == "claimed"


# ── a command given up on can still have left a training run behind ───────────


def _bury(cp_app, key: str) -> None:
    """Put a command in the state `_mark_dead` acts on, then bury it."""
    conn = cp_app._get_db()
    try:
        conn.execute(
            "INSERT INTO control_plane_commands (command_key, kind, request_hash, payload, state, "
            "attempts, created_at, updated_at) "
            "VALUES (?, 'retrain', 'hash', '{}', 'failed', 5, datetime('now'), datetime('now'))",
            (key,),
        )
        conn.commit()
    finally:
        conn.close()
    cp_app._mark_dead(key, 5)


def _row(cp_app, key: str) -> dict:
    conn = cp_app._get_db()
    try:
        row = conn.execute(
            "SELECT state, last_error FROM control_plane_commands WHERE command_key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return {"state": row["state"], "last_error": row["last_error"]}


def test_burying_a_command_says_so_when_prefect_has_a_run_for_it(cp, monkeypatch):
    """A dispatch that was merely slow lands after the platform has given up, so the work is
    recorded as dead while a training job for it runs. The partition drill measured exactly that
    (`tests/integration/test_control_plane_partition_kind_live.py`); until this, it was only
    findable by hand."""
    asked: list[str] = []

    class _Gate:
        def find_flow_run_by_key(self, key: str) -> str:
            asked.append(key)
            return "run-42"

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gate())
    _bury(cp, "v1:retrain:orphan")
    assert asked == ["v1:retrain:orphan"], "the command's own key is what Prefect deduplicates on"
    row = _row(cp, "v1:retrain:orphan")
    assert row["state"] == "dead"
    assert "run-42" in row["last_error"], row


def test_a_burial_does_not_depend_on_prefect_answering(cp, monkeypatch):
    """The lookup is a courtesy. If Prefect is down — which is *why* the command died, often —
    the command must still be dead, and no exception may escape."""

    class _Down:
        def find_flow_run_by_key(self, key: str) -> str:
            raise RuntimeError("no route to Prefect")

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Down())
    _bury(cp, "v1:retrain:prefect-down")
    row = _row(cp, "v1:retrain:prefect-down")
    assert row["state"] == "dead"
    assert not row["last_error"], "not knowing must not be recorded as a run"


def test_no_run_means_nothing_is_added(cp, monkeypatch):
    class _Empty:
        def find_flow_run_by_key(self, key: str) -> None:
            return None

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Empty())
    _bury(cp, "v1:retrain:clean")
    assert _row(cp, "v1:retrain:clean") == {"state": "dead", "last_error": None}


def test_a_healthy_replica_asks_about_a_command_another_replica_buried(cp, monkeypatch):
    """The reason the check cannot live only at the burial.

    The replica that gives up on a command is usually the one that cannot reach Prefect — so its own
    lookup times out too, and the orphan stays invisible. That is what the partition drill measured
    before this existed: a dead command whose `last_error` said only "Prefect unreachable". Every
    replica reconciles, so a healthy one asks on the dead replica's behalf.
    """

    class _Down:
        def find_flow_run_by_key(self, key: str) -> str:
            raise RuntimeError("no route to Prefect")

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Down())
    _bury(cp, "v1:retrain:swept")
    assert "flow run exists" not in (_row(cp, "v1:retrain:swept")["last_error"] or "")

    class _Healthy:
        def find_flow_run_by_key(self, key: str) -> str:
            return "run-99"

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Healthy())
    cp._ORPHAN_CHECKED.clear()  # a different replica has not asked yet
    cp._sweep_orphan_runs()
    assert "run-99" in (_row(cp, "v1:retrain:swept")["last_error"] or "")


def test_the_sweep_asks_again_until_the_window_closes(cp, monkeypatch):
    """The run it looks for appears *after* the burial, so one answer of "no run" cannot be final.

    An earlier version asked once per replica and the partition drill kept reporting no orphan:
    every replica had already asked before the held dispatch landed.
    """
    answers = iter([None, None, "run-late"])
    asks: list[str] = []

    class _EventuallyThere:
        def find_flow_run_by_key(self, key: str) -> str | None:
            asks.append(key)
            return next(answers, "run-late")

    monkeypatch.setattr(cp, "_get_gateway", lambda: _EventuallyThere())
    _bury(cp, "v1:retrain:late")
    for _ in range(3):
        cp._ORPHAN_CHECKED.clear()  # stand in for the recheck interval having passed
        cp._sweep_orphan_runs()
    assert len(asks) >= 3, asks
    assert "run-late" in (_row(cp, "v1:retrain:late")["last_error"] or "")


def test_the_sweep_asks_once_and_leaves_a_quiet_platform_alone(cp, monkeypatch):
    asks: list[str] = []

    class _Empty:
        def find_flow_run_by_key(self, key: str) -> None:
            asks.append(key)
            return None

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Empty())
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Empty())
    _bury(cp, "v1:retrain:quiet")
    cp._sweep_orphan_runs()
    cp._sweep_orphan_runs()
    cp._sweep_orphan_runs()
    # Once at the burial, once in the first sweep — never again while the answer stays "no run".
    assert asks.count("v1:retrain:quiet") <= 2, asks


def test_the_sweep_ignores_old_burials(cp, monkeypatch):
    """A window, so a platform that has been up for weeks does not re-ask about ancient commands."""
    asks: list[str] = []

    class _Counting:
        def find_flow_run_by_key(self, key: str) -> None:
            asks.append(key)
            return None

    monkeypatch.setattr(cp, "_get_gateway", lambda: _Counting())
    _bury(cp, "v1:retrain:ancient")
    conn = cp._get_db()
    try:
        conn.execute(
            "UPDATE control_plane_commands SET updated_at=datetime('now','-3 hours') "
            "WHERE command_key='v1:retrain:ancient'"
        )
        conn.commit()
    finally:
        conn.close()
    asks.clear()
    cp._ORPHAN_CHECKED.clear()
    cp._sweep_orphan_runs()
    assert asks == [], asks


def _retrain_metric(cp, outcome: str) -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "examlops_retrain_requests_total",
            {"model_name": "JPCP", "dataset_name": "PM100Dataset", "outcome": outcome},
        )
        or 0.0
    )


def test_a_dispatch_that_landed_is_not_counted_as_a_retrain_error(cp, monkeypatch):
    """A failed *bookkeeping* write must not be reported as a failed *retrain*.

    `_dispatch_flow_run` starts the training; `_complete_command` records it. If the second raises
    — a datastore blip, a superseded lease — the command is failed and retried, which is correct and
    self-heals (Prefect's idempotency key returns the same run). What was not correct is the metric:
    the `except` branch recorded `outcome="error"`, so a retrain that had **started** was counted as
    a retrain that failed.

    That is not a cosmetic ledger entry. `HighRetrainErrorRate` pages above a 20% error rate over
    15 minutes, and retrains are rare — one false error against one real retrain is 100%, and even
    after the retry succeeds it is 50%. A transient blip pages the on-call about a subsystem that
    is working.
    """
    gateway = _Gateway()
    monkeypatch.setattr(cp, "_get_gateway", lambda: gateway)

    def bookkeeping_fails(*_a, **_k):
        raise RuntimeError("datastore unreachable")

    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    key = (
        TestClient(cp.app)
        .post("/v1/retrain", json=body, headers=_headers("blip-1"))
        .json()["command_id"]
    )

    before_error = _retrain_metric(cp, "error")
    monkeypatch.setattr(cp, "_complete_command", bookkeeping_fails)
    assert cp._work_commands_once() == 1
    assert gateway.calls == [key], "the dispatch itself must have happened"

    assert _retrain_metric(cp, "error") == before_error, (
        "a retrain that started was counted as a retrain error; HighRetrainErrorRate pages on this"
    )
    # The command itself is still a failure — it is retried — and that stays visible.
    assert _state(cp, key)[0] == "failed"


def test_a_dispatch_that_never_happened_is_still_a_retrain_error(cp, monkeypatch):
    """Anti-vacuity: the metric must still see the failure it exists for."""

    class _BrokenGateway(_Gateway):
        def create_flow_run(self, *_a, **_k):
            raise RuntimeError("prefect unreachable")

    monkeypatch.setattr(cp, "_get_gateway", lambda: _BrokenGateway())
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    key = (
        TestClient(cp.app)
        .post("/v1/retrain", json=body, headers=_headers("broken-1"))
        .json()["command_id"]
    )
    before_error = _retrain_metric(cp, "error")
    assert cp._work_commands_once() == 1
    assert _retrain_metric(cp, "error") == before_error + 1, (
        "a dispatch that never reached Prefect is a real retrain error and must be counted"
    )
    assert _state(cp, key)[0] == "failed"
