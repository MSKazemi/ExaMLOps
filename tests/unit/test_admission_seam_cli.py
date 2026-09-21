"""`exa admission simulate` / `reservations` - read-only, on a real platform.db (ADR 0116)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_ADMISSION_POLICY", raising=False)
    import examlops.platform_db as pdb

    pdb.init_db()


def _run(*args):
    return runner.invoke(app, ["--json", "admission", *args])


def _req(tmp_path, **kw):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({"project": "p", "tenant": "a", **kw}))
    return str(p)


def test_simulate_default_policy_admits_and_lists_ignored_fields(tmp_path):
    r = _run("simulate", "--request", _req(tmp_path, gang=True, resources={"gpus": 4}))
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["policy"] == "fair-share" and out["verdict"] == "admit"
    assert {"gang", "resources.gpus"} <= set(out["ignored_fields"])


def test_simulate_baseline_policy_refuses_gang_on_an_unproven_backend(tmp_path):
    r = _run(
        "simulate",
        "--request",
        _req(tmp_path, gang=True, resources={"gpus": 4}),
        "--policy",
        "baseline-over-quota",
    )
    out = json.loads(r.stdout)
    assert out["verdict"] == "reject" and "gang" in out["reason"]


def test_simulate_writes_nothing(tmp_path):
    import examlops.platform_db as pdb

    _run("simulate", "--request", _req(tmp_path), "--policy", "baseline-over-quota")
    with pdb.get_db() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM audit_events WHERE action='admission_decision'"
            ).fetchone()["c"]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) c FROM quota_reservations").fetchone()["c"] == 0


def test_simulate_cluster_state_override_and_bad_input(tmp_path):
    st = tmp_path / "st.json"
    st.write_text(json.dumps({"total_gpus": 8, "free_gpus": 0}))
    r = _run(
        "simulate",
        "--request",
        _req(tmp_path, resources={"gpus": 2}),
        "--policy",
        "baseline-over-quota",
        "--cluster-state",
        str(st),
    )
    assert json.loads(r.stdout)["verdict"] == "queue"
    bad = _run("simulate", "--request", _req(tmp_path, gpuz=1))
    assert bad.exit_code == 1


def test_reservations_list_and_expire_preview():
    from examlops.data import quota_reservations as store

    store.reserve("live", "p", gpus=1, ttl_s=10_000, now=None)
    store.reserve("dead", "p", gpus=1, ttl_s=1.0, now=1.0)
    listed = json.loads(_run("reservations").stdout)["reservations"]
    assert {r["id"] for r in listed} == {"live", "dead"}
    assert next(r for r in listed if r["id"] == "dead")["lapsed"] is True
    prev = json.loads(_run("reservations", "--expire-preview").stdout)
    assert [r["id"] for r in prev["reservations"]] == ["dead"]
    assert store.get("dead")["state"] == "reserved", "preview must not change anything"
