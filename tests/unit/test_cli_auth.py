"""ADR 0120 — `exa auth`: device-flow sign-in against a real-HTTP fake IdP, and the operator tools.

The session is hermetic: `EXAMLOPS_CONFIG` points into tmp_path, so credentials.json lands there
and nothing touches the developer's real `~/.config/examlops`.
"""

from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from examlops import iam
from examlops.cli.main import app
from tests.unit._iam_fakes import FakeIdP

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    idp = FakeIdP()
    idp.device_pending = 0  # approve on the first poll: one 1-second interval, not three
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    for var in (
        "CONTROL_PLANE_TOKEN",
        "DASHBOARD_TOKEN",
        "EXAMLOPS_AUTH_ISSUER",
        "EXAMLOPS_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    trust = tmp_path / "identity-providers.yaml"
    trust.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "jsc",
                        "display_name": "Jülich Supercomputing Centre",
                        "issuer": idp.issuer,
                        "audience": "examlops",
                        "tenant": "jsc",
                        "role_rules": [{"value": "examlops-operators", "role": "operator"}],
                        "clients": {"cli": {"client_id": "exa-cli"}},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(trust))
    iam.clear_caches()
    yield idp, trust
    idp.stop()
    iam.clear_caches()


def _json(*args: str) -> dict:
    res = runner.invoke(app, ["--json", *args])
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def test_device_login_then_every_call_carries_the_users_identity(env):
    idp, _ = env
    res = runner.invoke(app, ["auth", "login", "--provider", "jsc"])
    assert res.exit_code == 0, res.output
    assert "ABCD-EFGH" in res.output and "Jülich Supercomputing Centre" in res.output
    status = _json("auth", "status")
    assert status["signed_in"] and status["provider"] == "jsc" and status["refreshable"]
    who = _json("auth", "whoami")
    assert who["verified"] and who["role"] == "operator" and who["tenant"] == "jsc"
    # The control-plane/dashboard clients pick the session token up with no further config.
    from examlops.cli._config import load_config

    cfg = load_config()
    assert cfg.control_plane_token and cfg.control_plane_token == cfg.dashboard_token
    token = runner.invoke(app, ["auth", "token"]).stdout.strip()
    assert token == cfg.control_plane_token
    assert iam.verify_access_token(token).id == "jsc:u-123"


def test_a_configured_static_token_still_wins(env, monkeypatch):
    runner.invoke(app, ["auth", "login", "--provider", "jsc"])
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "static-operator-token-123")
    from examlops.cli._config import load_config

    assert load_config().control_plane_token == "static-operator-token-123"


def test_login_with_a_bare_issuer_and_logout(env):
    idp, _ = env
    res = runner.invoke(app, ["auth", "login", "--issuer", idp.issuer, "--client-id", "exa-cli"])
    assert res.exit_code == 0, res.output
    assert _json("auth", "status")["issuer"] == idp.issuer
    out = _json("auth", "logout")
    assert out["signed_out"] is True
    assert _json("auth", "status")["signed_in"] is False
    assert runner.invoke(app, ["auth", "token"]).exit_code != 0


def test_providers_validate_verify_decide(env, tmp_path):
    idp, trust = env
    rows = _json("auth", "providers")["providers"]
    assert rows[0]["name"] == "jsc" and rows[0]["clients"] == ["cli"]
    assert _json("auth", "validate")["valid"] is True
    token_file = tmp_path / "t.jwt"
    token_file.write_text(idp.mint(groups=["examlops-operators"]))
    verified = _json("auth", "verify", "--token-file", str(token_file))
    assert verified["valid"] and verified["role"] == "operator"
    allow = _json("auth", "decide", "retrain.trigger", "--token-file", str(token_file))
    assert allow["allowed"] is True and allow["layer"] == "local"
    deny = runner.invoke(
        app, ["--json", "auth", "decide", "secrets.manage", "--token-file", str(token_file)]
    )
    assert deny.exit_code == 1 and json.loads(deny.stdout)["allowed"] is False
    cross = runner.invoke(
        app,
        ["--json", "auth", "decide", "view", "--tenant", "cineca", "--token-file", str(token_file)],
    )
    assert cross.exit_code == 1 and json.loads(cross.stdout)["layer"] == "tenant"


def test_validate_fails_on_a_bad_trust_file(env, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {"providers": [{"name": "x", "issuer": "http://idp.example.org", "audience": "a"}]}
        )
    )
    res = runner.invoke(app, ["--json", "auth", "validate", "--file", str(bad)])
    assert res.exit_code == 1
    assert any("plain HTTP" in e for e in json.loads(res.stdout)["errors"])


def test_verify_rejects_a_token_from_an_untrusted_issuer(env, tmp_path):
    stranger = FakeIdP(issuer_suffix="/realms/stranger")
    try:
        f = tmp_path / "s.jwt"
        f.write_text(stranger.mint())
        res = runner.invoke(app, ["--json", "auth", "verify", "--token-file", str(f)])
    finally:
        stranger.stop()
    assert res.exit_code == 1 and "not a trusted" in json.loads(res.stdout)["error"]


def test_accounts_deactivate_and_activate(env):
    """ADR 0132: an operator can cut a federated account off at once, and undo it."""
    idp, _ = env
    token = idp.mint(groups=["examlops-operators"])
    iam.verify_access_token(token)  # first sight records the account (JIT)
    listed = _json("auth", "accounts", "--provider", "jsc")
    assert listed["total"] == 1 and listed["accounts"][0]["username"] == "alice"
    off = _json("auth", "deactivate", "alice", "--provider", "jsc", "--reason", "left")
    assert off["active"] is False
    assert _json("auth", "accounts", "--inactive")["total"] == 1
    with pytest.raises(iam.AuthenticationError, match="deactivated"):
        iam.verify_access_token(token)
    assert _json("auth", "activate", "alice", "--provider", "jsc")["active"] is True
    assert iam.verify_access_token(token).subject == "u-123"
    missing = runner.invoke(app, ["auth", "deactivate", "nobody", "--provider", "jsc"])
    assert missing.exit_code != 0
