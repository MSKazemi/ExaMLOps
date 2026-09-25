"""ADR 0011 clause 2 — services resolve secret references at startup instead of reading plaintext.

Covers :mod:`examlops.secrets.inject` (resolution, fail-closed, idempotency, bounds, audit) and
its wiring into the real service entry points: the control plane resolves before it reads
``CONTROL_PLANE_TOKEN``, the dashboard before ``Settings()``, the agent before ``skipper`` loads,
and the dataplane / LLM gateway entrypoints before ``create_app``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from examlops import secrets as sec
from examlops.platform_db import get_db, init_db
from examlops.secrets import inject

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEY", Fernet.generate_key().decode())
    for var in (
        "EXAMLOPS_VAULT_ADDR",
        "EXAMLOPS_SOPS_FILE",
        "EXAMLOPS_SECRETS_INJECT",
        "EXAMLOPS_SECRETS_INJECT_STRICT",
        "EXAMLOPS_SECRETS_WRITE_BACKEND",
        "EXAMLOPS_SECRET_TENANTS",
    ):
        monkeypatch.delenv(var, raising=False)
    init_db()


def _audit(action: str) -> list[tuple[str, dict]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT actor, details FROM audit_events WHERE action=?", (action,)
        ).fetchall()
    return [(r["actor"], json.loads(r["details"] or "{}")) for r in rows]


def test_parse_ref_path_and_tenant():
    assert inject.parse_ref("secret://control-plane/token") == ("control-plane/token", "default")
    assert inject.parse_ref("secret://acme/api-key?tenant=acme") == ("acme/api-key", "acme")
    for bad in ("secret://", "secret://a//b", "secret://../x", "http://x"):
        with pytest.raises(ValueError):
            inject.parse_ref(bad)


def test_manager_reference_is_resolved_in_place_and_audited():
    sec.set_secret("control-plane/token", "tok-123", actor="op")
    env = {"CONTROL_PLANE_TOKEN": "secret://control-plane/token", "OTHER": "plain"}
    report = inject.inject_env("control-plane", env)
    assert env == {"CONTROL_PLANE_TOKEN": "tok-123", "OTHER": "plain"}
    assert [(i.name, i.backend) for i in report.injected] == [("CONTROL_PLANE_TOKEN", "local")]
    # the read is an audited access by the service; the summary names the variable, not the value
    assert any(
        actor == "service:control-plane" and d.get("backend") == "local"
        for actor, d in _audit("secret_access")
    )
    actor, details = _audit("secrets_injected")[-1]
    assert actor == "service:control-plane"
    assert details["injected"] == [["CONTROL_PLANE_TOKEN", "local"]]
    assert "tok-123" not in json.dumps(details)


def test_injection_is_idempotent():
    sec.set_secret("a/b", "v", actor="op")
    env = {"X_TOKEN": "secret://a/b"}
    inject.inject_env("svc", env)
    second = inject.inject_env("svc", env)
    assert env["X_TOKEN"] == "v" and second.injected == [] and second.errors == []
    assert len(_audit("secrets_injected")) == 1


def test_no_reference_is_a_noop_without_touching_the_audit_log():
    env = {"A": "1"}
    report = inject.inject_env("svc", env)
    assert env == {"A": "1"} and report.injected == []
    assert _audit("secrets_injected") == [] and _audit("secrets_inject_failed") == []


def test_unresolvable_reference_refuses_to_start_without_leaking_values():
    sec.set_secret("ok/one", "visible-value", actor="op")
    env = {"GOOD": "secret://ok/one", "BAD_TOKEN": "secret://missing/one"}
    with pytest.raises(inject.SecretInjectionError) as exc:
        inject.inject_env("svc", env)
    assert "BAD_TOKEN" in str(exc.value) and "visible-value" not in str(exc.value)
    assert _audit("secrets_inject_failed")[-1][1]["failed"] == ["BAD_TOKEN"]


def test_non_strict_removes_the_unresolved_variable_never_leaves_the_literal(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_INJECT_STRICT", "0")
    env = {"BAD_TOKEN": "secret://missing/one"}
    report = inject.inject_env("svc", env)
    assert "BAD_TOKEN" not in env
    assert report.removed == ["BAD_TOKEN"]


def test_tenant_scoped_reference_is_denied_for_the_wrong_tenant(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRET_TENANTS", "acme,globex")
    sec.set_secret("acme/key", "acme-v", tenant="acme", actor="op")
    env = {"K": "secret://acme/key?tenant=globex"}
    with pytest.raises(inject.SecretInjectionError, match="SecretAccessDenied"):
        inject.inject_env("svc", env)
    env = {"K": "secret://acme/key?tenant=acme"}
    inject.inject_env("svc", env)
    assert env["K"] == "acme-v"


def test_file_reference_reads_a_mounted_secret(tmp_path):
    f = tmp_path / "jwt"
    f.write_text("file-secret\n")
    env = {"DASHBOARD_JWT_SECRET": f"secret+file://{f}"}
    report = inject.inject_env("dashboard", env)
    assert env["DASHBOARD_JWT_SECRET"] == "file-secret"  # one trailing newline cut
    assert report.injected[0].kind == "file" and report.injected[0].target == str(f)


def test_file_reference_bounds(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * (inject.MAX_FILE_BYTES + 1))
    for value in (
        f"secret+file://{big}",
        "secret+file://relative/path",
        f"secret+file://{tmp_path}",
    ):
        with pytest.raises(inject.SecretInjectionError):
            inject.inject_env("svc", {"V": value})


def test_reference_count_is_capped():
    env = {f"V{i}": "secret://a/b" for i in range(inject.MAX_REFS + 1)}
    with pytest.raises(inject.SecretInjectionError, match="exceeds the limit"):
        inject.inject_env("svc", env)


def test_disabled_removes_references_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("EXAMLOPS_SECRETS_INJECT", "0")
    env = {"V": "secret://a/b"}
    inject.inject_env("svc", env)
    assert "V" not in env  # never left as a literal a token check would accept
    assert "removed unresolved" in caplog.text


def test_classify_env_flags_plaintext_and_keeps_bootstrap_apart():
    rows = {
        r["name"]: r
        for r in inject.classify_env(
            {
                "CONTROL_PLANE_TOKEN": "secret://cp/token?tenant=x",
                "DASHBOARD_JWT_SECRET": "secret+file:///run/secrets/jwt",
                "DASHBOARD_ADMIN_PASSWORD": "hunter2",
                "EXAMLOPS_VAULT_TOKEN": "bootstrap",
                "EXAMLOPS_IAM_TOKEN_FILE": "/x",  # a path to a credential is not the credential
                "MLFLOW_TRACKING_URI": "http://m",
                "EMPTY_TOKEN": "",
            }
        )
    }
    assert rows["CONTROL_PLANE_TOKEN"] == {
        "name": "CONTROL_PLANE_TOKEN",
        "kind": "reference",
        "target": "secret://cp/token",
    }
    assert rows["DASHBOARD_JWT_SECRET"]["kind"] == "file"
    assert rows["DASHBOARD_ADMIN_PASSWORD"]["kind"] == "plaintext"
    assert rows["EXAMLOPS_VAULT_TOKEN"]["kind"] == "bootstrap"
    assert set(rows) == {
        "CONTROL_PLANE_TOKEN",
        "DASHBOARD_JWT_SECRET",
        "DASHBOARD_ADMIN_PASSWORD",
        "EXAMLOPS_VAULT_TOKEN",
    }
    assert "hunter2" not in json.dumps(list(rows.values()))


def test_parse_env_file():
    text = "# c\nexport A=1\nB='two words'\nC=\"q\"\nD=val # trailing\nnoeq\n"
    assert inject.parse_env_file(text) == {"A": "1", "B": "two words", "C": "q", "D": "val"}


# ─── wiring into the real service entry points ────────────────────────────────


def _run(code: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    full = {
        **{k: v for k, v in os.environ.items() if not k.startswith("EXAMLOPS_SECRETS_INJECT")},
        **env,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "platform" / "cli" / "src"), str(ROOT)]),
    }
    return subprocess.run(
        [sys.executable, "-c", code], env=full, cwd=cwd, capture_output=True, text=True, timeout=120
    )


def test_control_plane_resolves_its_token_before_reading_it(tmp_path):
    sec.set_secret("control-plane/token", "cp-injected-token-value", actor="op")
    code = (
        "import sys; sys.path.insert(0, 'platform/services/control_plane'); "
        "import app; print('TOKEN=' + app.CONTROL_PLANE_TOKEN)"
    )
    env = {
        "PLATFORM_DB": os.environ["PLATFORM_DB"],
        "EXAMLOPS_SECRETS_KEY": os.environ["EXAMLOPS_SECRETS_KEY"],
        "CONTROL_PLANE_TOKEN": "secret://control-plane/token",
    }
    proc = _run(code, env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "TOKEN=cp-injected-token-value" in proc.stdout


def test_control_plane_refuses_to_start_on_an_unresolvable_reference():
    code = "import sys; sys.path.insert(0, 'platform/services/control_plane'); import app"
    env = {
        "PLATFORM_DB": os.environ["PLATFORM_DB"],
        "EXAMLOPS_SECRETS_KEY": os.environ["EXAMLOPS_SECRETS_KEY"],
        "CONTROL_PLANE_TOKEN": "secret://nowhere/token",
    }
    proc = _run(code, env, cwd=ROOT)
    assert proc.returncode != 0
    assert "SecretInjectionError" in proc.stderr and "CONTROL_PLANE_TOKEN" in proc.stderr


def test_dashboard_settings_resolve_references_before_settings(tmp_path):
    sec.set_secret("dashboard/jwt", "j" * 40, actor="op")
    backend = ROOT / "platform" / "services" / "dashboard" / "backend"
    code = "import settings; print('JWT=' + settings.settings.dashboard_jwt_secret)"
    env = {
        "PLATFORM_DB": os.environ["PLATFORM_DB"],
        "EXAMLOPS_SECRETS_KEY": os.environ["EXAMLOPS_SECRETS_KEY"],
        "DASHBOARD_JWT_SECRET": "secret://dashboard/jwt",
        "DASHBOARD_VIEWER_PASSWORD": "v",
        "DASHBOARD_ADMIN_PASSWORD": "a",
        "DASHBOARD_SECRET_KEY": Fernet.generate_key().decode(),
    }
    proc = _run(code, env, cwd=backend)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "JWT=" + "j" * 40 in proc.stdout


def test_agent_entrypoint_resolves_before_skipper_loads(monkeypatch):
    sec.set_secret("agent/key", "agent-key-value", actor="op")
    code = (
        "import sys, os; sys.path.insert(0, 'platform/services/agent'); import agent_server; "
        "agent_server.inject_secret_refs(); print('KEY=' + os.environ['AGENT_API_KEY'])"
    )
    env = {
        "PLATFORM_DB": os.environ["PLATFORM_DB"],
        "EXAMLOPS_SECRETS_KEY": os.environ["EXAMLOPS_SECRETS_KEY"],
        "AGENT_API_KEY": "secret://agent/key",
    }
    proc = _run(code, env, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "KEY=agent-key-value" in proc.stdout


@pytest.mark.parametrize(
    "entry,service",
    [
        ("platform/services/dataplane/main.py", "dataplane"),
        ("platform/services/llm_gateway/main.py", "llm-gateway"),
    ],
)
def test_service_entrypoints_inject_before_create_app(entry, service):
    """Structural: the call sits in ``__main__`` ahead of ``create_app()``."""
    text = (ROOT / entry).read_text()
    main = text.split('if __name__ == "__main__":', 1)[1]
    assert f'inject_env("{service}")' in main
    assert main.index(f'inject_env("{service}")') < main.index("create_app()")
