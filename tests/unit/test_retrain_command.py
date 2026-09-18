"""Retrains go through the control plane's command API and wait for the dispatch (plan P1.6c).

``examlops.retrain_command`` replaced eleven callers of the deprecated synchronous
``POST /retrain``. What it must guarantee: a retrain dispatched in time answers like the old route
(a ``flow_run_id``); one still queued when the wait ends is reported as accepted — never as an
error, because the control plane will still dispatch it; one the control plane gave up on raises
the ``ClientError`` every caller already handles; and the wait is bounded.
"""

from __future__ import annotations

import asyncio

import pytest

from examlops import retrain_command
from examlops.cli._client import ClientError

CID = "v1:retrain:abc"


def _view(state: str, **extra) -> dict:
    return {
        "command_id": CID,
        "kind": "retrain",
        "state": state,
        "attempts": 1,
        "result": None,
        "last_error": None,
        "status_url": f"/v1/commands/{CID}",
        **extra,
    }


DISPATCHED = _view("succeeded", result={"flow_run_id": "run-7", "deployment": "training_flow/x"})


class _Fetch:
    def __init__(self, *views):
        self.views = list(views)
        self.calls = 0

    def __call__(self, command_id: str) -> dict:
        assert command_id == CID
        self.calls += 1
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]


def test_a_dispatched_retrain_answers_like_the_old_route():
    answer = retrain_command.follow(_view("pending"), _Fetch(DISPATCHED), sleep=lambda _s: None)
    assert answer["flow_run_id"] == "run-7" and answer["deployment"] == "training_flow/x"
    assert answer["status_url"] == "/v1/runs/run-7"
    assert answer["command_id"] == CID and answer["state"] == "succeeded"
    assert retrain_command.dispatched(answer)


def test_an_already_terminal_view_is_not_polled():
    fetch = _Fetch(DISPATCHED)
    retrain_command.follow(DISPATCHED, fetch, sleep=lambda _s: None)
    assert fetch.calls == 0


def test_a_retrain_still_queued_is_accepted_not_an_error_and_the_wait_is_bounded():
    slept: list[float] = []
    fetch = _Fetch(_view("failed", last_error="prefect unreachable"))
    answer = retrain_command.follow(_view("pending"), fetch, wait=3.0, sleep=slept.append)
    assert not retrain_command.dispatched(answer)
    assert answer["state"] == "failed" and answer["last_error"] == "prefect unreachable"
    assert answer["status_url"] == f"/v1/commands/{CID}"
    assert sum(slept) == pytest.approx(3.0)
    assert max(slept) <= 2.0 and slept[0] == 0.25  # backs off, but keeps looking


@pytest.mark.parametrize("state", ["dead", "cancelled"])
def test_a_retrain_given_up_on_raises_the_error_callers_handle(state):
    with pytest.raises(ClientError, match=f"{CID} {state}: boom"):
        retrain_command.follow(
            _view("pending"), _Fetch(_view(state, last_error="boom")), sleep=lambda _s: None
        )


def test_zero_wait_returns_the_submission_as_is():
    fetch = _Fetch(DISPATCHED)
    answer = retrain_command.follow(_view("pending"), fetch, wait=0, sleep=lambda _s: None)
    assert fetch.calls == 0 and answer["state"] == "pending"


def test_the_async_variant_follows_the_same_rules(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(retrain_command.asyncio, "sleep", no_sleep)

    async def fetch(_cid):
        return DISPATCHED

    answer = asyncio.run(retrain_command.follow_async(_view("pending"), fetch))
    assert answer["flow_run_id"] == "run-7"


def test_submit_goes_to_the_command_api_with_the_callers_key(monkeypatch):
    from examlops import control_plane_api

    seen: dict = {}

    def submit_retrain(**kwargs):
        seen.update(kwargs)
        return _view("pending")

    monkeypatch.setattr(control_plane_api, "submit_retrain", submit_retrain)
    monkeypatch.setattr(control_plane_api, "get_command", lambda cid, **_k: DISPATCHED)
    monkeypatch.setattr(retrain_command.time, "sleep", lambda _s: None)

    answer = retrain_command.submit(
        {"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        idempotency_key="k1",
        base="http://cp:8002",
        token="t",
    )
    assert answer["flow_run_id"] == "run-7"
    assert seen["idempotency_key"] == "k1" and seen["base"] == "http://cp:8002"
    assert seen["body"]["model_name"] == "JPCP"
