"""Config validation (enterprise-readiness Phase 4, item 4.2 slice).

Proves `exa env --validate` catches incoherent deployments: a backend selected without its endpoint,
OIDC without a JWKS, malformed URLs, and weak/placeholder secrets — while a clean config passes.
"""

from __future__ import annotations

from examlops.config_validate import has_errors, validate


def _levels(findings, key):
    return {f.level for f in findings if f.key == key}


def test_clean_config_has_no_errors():
    findings = validate({"CONTROL_PLANE_URL": "http://cp:8002", "CONTROL_PLANE_TOKEN": "x" * 40})
    assert not has_errors(findings)
    assert any(f.level == "ok" for f in findings)


def test_bad_url_is_error():
    findings = validate({"CONTROL_PLANE_URL": "not-a-url"})
    assert has_errors(findings) and "error" in _levels(findings, "CONTROL_PLANE_URL")


def test_redis_coordinator_without_url_is_error():
    findings = validate({"EXAMLOPS_COORDINATOR": "redis"})
    assert has_errors(findings)


def test_nats_publisher_is_rejected_until_implemented():
    findings = validate({"EXAMLOPS_EVENT_PUBLISHER": "nats"})
    assert has_errors(findings)
    assert has_errors(
        validate({"EXAMLOPS_EVENT_PUBLISHER": "nats", "EXAMLOPS_NATS_URL": "nats://n:4222"})
    )


def test_redis_publisher_requires_endpoint_and_is_implemented():
    assert has_errors(validate({"EXAMLOPS_EVENT_PUBLISHER": "redis"}))
    assert not has_errors(
        validate({"EXAMLOPS_EVENT_PUBLISHER": "redis", "EXAMLOPS_REDIS_URL": "redis://r:6379"})
    )


def test_unknown_coordination_and_event_backends_are_errors():
    assert has_errors(validate({"EXAMLOPS_COORDINATOR": "typo"}))
    assert has_errors(validate({"EXAMLOPS_EVENT_PUBLISHER": "typo"}))


def test_event_limits_must_be_positive_integers():
    assert has_errors(validate({"EXAMLOPS_EVENT_MAX_ATTEMPTS": "0"}))
    assert has_errors(validate({"EXAMLOPS_REDIS_EVENT_MAXLEN": "many"}))
    assert not has_errors(
        validate({"EXAMLOPS_EVENT_MAX_ATTEMPTS": "3", "EXAMLOPS_REDIS_EVENT_MAXLEN": "100"})
    )


def test_postgres_backend_without_dsn_is_error():
    assert has_errors(validate({"EXAMLOPS_DB_BACKEND": "postgres"}))
    assert not has_errors(
        validate({"EXAMLOPS_DB_BACKEND": "postgres", "DATABASE_URL": "postgres://…"})
    )


def test_oidc_issuer_without_jwks_is_error():
    findings = validate({"EXAMLOPS_OIDC_ISSUER": "https://idp/"})
    assert has_errors(findings) and "error" in _levels(findings, "EXAMLOPS_OIDC_JWKS")


def test_placeholder_token_warns():
    findings = validate({"CONTROL_PLANE_TOKEN": "changeme"})
    assert "warn" in _levels(findings, "CONTROL_PLANE_TOKEN")
    assert not has_errors(findings)  # a warning is not a hard error


def test_weak_dashboard_secret_warns():
    findings = validate({"DASHBOARD_JWT_SECRET": "short"})
    assert "warn" in _levels(findings, "DASHBOARD_JWT_SECRET")


def test_cli_env_validate_exits_nonzero_on_error(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_COORDINATOR", "redis")
    monkeypatch.delenv("EXAMLOPS_REDIS_URL", raising=False)
    r = CliRunner().invoke(app, ["env", "--validate"])
    assert r.exit_code == 1, r.output
