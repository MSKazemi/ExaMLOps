"""Policy-as-code on the dashboard's write routes (ADR 0079 d2 / ADR 0029 d3).

Driven through the real app: the gate is installed by ``main.py`` after the router table is built,
so these tests exercise the same dependency ordering production has.
"""

import sqlite3

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW

_NEW = {"name": "gated", "cpuLimit": 1, "memoryLimitGb": 1, "storageGb": 1, "gpuLimit": 0}


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    dbconn.connect(db, row_factory=None).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    """Write a policy.yaml the engine will read; with no call, there is no policy file at all."""
    path = tmp_path / "policy.yaml"
    monkeypatch.setattr("examlops.policy.POLICY_YAML", path)

    def write(text: str) -> None:
        path.write_text(text)

    return write


async def _tok(client, pw):
    return (await client.post("/api/auth/login", json={"password": pw})).json()["token"]


def _h(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def _audit(db, prefix):
    conn = dbconn.connect(db, row_factory=None)
    try:
        return conn.execute(
            "SELECT action, actor, source, details FROM audit_events WHERE action LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
    except sqlite3.OperationalError:  # no audit table yet: nothing has ever been written
        return []
    finally:
        conn.close()


async def test_default_is_unchanged_and_writes_no_policy_audit(client, platform_db, policy_file):
    """No policy file: the route answers as before and leaves no policy trace."""
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code in (200, 201), r.text
    assert _audit(platform_db, "policy") == []


async def test_non_matching_rule_is_also_unchanged(client, platform_db, policy_file):
    policy_file("policies:\n  - action: some_other_action\n    effect: deny\n")
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code in (200, 201), r.text
    assert _audit(platform_db, "policy") == []


async def test_deny_is_403_names_the_rule_and_is_audited_before_the_mutation(
    client, platform_db, policy_file
):
    policy_file(
        "policies:\n  - name: freeze-projects\n    action: dashboard_projects_create\n"
        "    effect: deny\n"
    )
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code == 403
    assert "freeze-projects" in r.json()["detail"]
    assert r.headers["x-policy-rule"] == "freeze-projects"
    rows = _audit(platform_db, "policy:dashboard_projects_create")
    assert len(rows) == 1
    assert rows[0][2] == "dashboard-policy"
    assert "deny" in rows[0][3] and "freeze-projects" in rows[0][3]
    # The mutation did not happen.
    lr = await client.get("/api/v1/projects", headers=_h(tok))
    assert all(p["name"] != "gated" for p in lr.json())


async def test_condition_sees_the_body_and_path_context(client, platform_db, policy_file):
    """A rule can discriminate on the request: only the named project is refused."""
    policy_file(
        "policies:\n  - name: no-prod\n    action: dashboard_projects_create\n"
        "    when: \"body_name == 'prod'\"\n    effect: deny\n"
    )
    tok = await _tok(client, ADMIN_PW)
    assert (
        await client.post("/api/v1/projects", json={**_NEW, "name": "prod"}, headers=_h(tok))
    ).status_code == 403
    assert (await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))).status_code in (
        200,
        201,
    )


async def test_shared_cli_action_name_project_delete(client, platform_db, policy_file):
    """`project_delete` is the CLI's own action: one rule governs both doors."""
    tok = await _tok(client, ADMIN_PW)
    assert (await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))).status_code in (
        200,
        201,
    )
    policy_file("policies:\n  - name: keep\n    action: project_delete\n    effect: deny\n")
    r = await client.delete("/api/v1/projects/gated", headers=_h(tok))
    assert r.status_code == 403 and "keep" in r.json()["detail"]
    lr = await client.get("/api/v1/projects", headers=_h(tok))
    assert any(p["name"] == "gated" for p in lr.json())


async def test_require_approval_needs_the_acknowledgement_then_proceeds(
    client, platform_db, policy_file
):
    policy_file(
        "policies:\n  - name: four-eyes\n    action: dashboard_projects_create\n"
        "    effect: require_approval\n"
    )
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code == 409
    assert "four-eyes" in r.json()["detail"] and "X-Policy-Approved" in r.json()["detail"]
    lr = await client.get("/api/v1/projects", headers=_h(tok))
    assert all(p["name"] != "gated" for p in lr.json())  # nothing changed

    r2 = await client.post(
        "/api/v1/projects", json=_NEW, headers=_h(tok, **{"X-Policy-Approved": "true"})
    )
    assert r2.status_code in (200, 201), r2.text
    approvals = _audit(platform_db, "policy_approval:dashboard_projects_create")
    assert len(approvals) == 1 and approvals[0][1].startswith("admin")  # the human who confirmed


async def test_monitor_mode_rule_never_blocks_but_is_audited(client, platform_db, policy_file):
    policy_file(
        "policies:\n  - name: trial\n    action: dashboard_projects_create\n"
        "    effect: deny\n    mode: monitor\n"
    )
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code in (200, 201), r.text
    rows = _audit(platform_db, "policy_monitor:dashboard_projects_create")
    assert len(rows) == 1 and "would_effect" in rows[0][3] and "trial" in rows[0][3]


async def test_engine_failure_denies_and_is_audited(client, platform_db, policy_file, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("examlops.policy.decide", boom)
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code == 403 and "unavailable" in r.json()["detail"]
    assert len(_audit(platform_db, "policy_unavailable:dashboard_projects_create")) == 1
    lr_ok = await client.get("/api/v1/projects", headers=_h(tok))
    assert all(p["name"] != "gated" for p in lr_ok.json())


async def test_no_rule_leaves_the_capability_403_exactly_as_it_was(client, platform_db):
    """No policy: the gate allows silently and the route's own capability check still answers."""
    tok = await _tok(client, VIEWER_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code == 403
    assert "policy" not in r.json()["detail"].lower()
    assert _audit(platform_db, "policy") == []


async def test_a_deny_rule_answers_a_viewer_before_the_capability_check(
    client, platform_db, policy_file
):
    """Precedence: the app-level gate runs first, so a matching rule answers (403 either way)."""
    policy_file(
        "policies:\n  - name: any\n    action: dashboard_projects_create\n    effect: deny\n"
    )
    tok = await _tok(client, VIEWER_PW)
    r = await client.post("/api/v1/projects", json=_NEW, headers=_h(tok))
    assert r.status_code == 403 and r.headers["x-policy-rule"] == "any"


async def test_a_non_admin_is_never_invited_to_approve(client, platform_db, policy_file):
    """require_approval is 409 for an admin only; anyone else is refused, never let through."""
    policy_file(
        "policies:\n  - name: q\n    action: dashboard_projects_create\n"
        "    effect: require_approval\n"
    )
    tok = await _tok(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/projects", json=_NEW, headers=_h(tok, **{"X-Policy-Approved": "true"})
    )
    assert r.status_code == 403 and "admin" in r.json()["detail"]


async def test_unauthenticated_gated_request_is_the_normal_401(client, policy_file):
    policy_file("policies:\n  - action: '*'\n    effect: deny\n")
    r = await client.post("/api/v1/projects", json=_NEW)
    assert r.status_code == 401 and "policy" not in r.text.lower()


async def test_ungated_and_safe_requests_are_untouched_by_a_catch_all_deny(client, policy_file):
    """Login (exempt, unauthenticated) and reads never reach the engine."""
    policy_file("policies:\n  - action: '*'\n    effect: deny\n")
    r = await client.post("/api/auth/login", json={"password": ADMIN_PW})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert (await client.get("/api/v1/projects", headers=_h(tok))).status_code == 200


async def test_manual_promote_gate_carries_cli_vocabulary(client, platform_db, policy_file):
    """The alias route reuses `manual_promote` with `to_alias`/`model`, as `exa pipeline promote`."""
    policy_file(
        "policies:\n  - name: prod-frozen\n    action: manual_promote\n"
        "    when: \"to_alias == 'Production' and model == 'JPCP'\"\n    effect: deny\n"
    )
    tok = await _tok(client, ADMIN_PW)
    r = await client.put(
        "/api/models/JPCP/versions/3/alias", json={"alias": "Production"}, headers=_h(tok)
    )
    assert r.status_code == 403 and "prod-frozen" in r.json()["detail"]
    assert _audit(platform_db, "policy:manual_promote")


async def test_fleet_approve_reuses_cluster_approve(client, platform_db, policy_file):
    policy_file(
        "policies:\n  - name: hold\n    action: cluster_approve\n"
        "    when: \"cluster == 'lxp'\"\n    effect: deny\n"
    )
    tok = await _tok(client, ADMIN_PW)
    r = await client.post("/api/v1/facility/fleet/lxp/approve", headers=_h(tok))
    assert r.status_code == 403 and "hold" in r.json()["detail"]


async def test_secret_value_never_reaches_context_or_audit(client, platform_db, policy_file):
    from policy_gate import _body_scalars

    ctx = _body_scalars(b'{"path": "a/b", "value": "hunter2", "api_key": "k", "n": 3}')
    assert ctx == {"path": "a/b", "n": 3}
