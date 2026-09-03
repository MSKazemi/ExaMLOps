"""Unit tests for password compare, JWT issue/verify, role gate."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException


def test_check_password_matches_viewer():
    from auth import check_password

    assert check_password("test-viewer-pw") == "viewer"


def test_check_password_matches_admin():
    from auth import check_password

    assert check_password("test-admin-pw") == "admin"


def test_check_password_rejects_unknown():
    from auth import check_password

    assert check_password("nope") is None


def test_check_password_rejects_empty():
    from auth import check_password

    assert check_password("") is None


def test_issue_and_verify_token_round_trip():
    from auth import issue_token, verify_token

    token, expires_at = issue_token("admin")
    payload = verify_token(token)
    assert payload["role"] == "admin"
    assert len(payload["jti"]) == 32
    # D6: a stable actor id so audit events do not record "?" as the actor.
    assert payload["sub"] == f"admin@{payload['jti'][:8]}"
    assert isinstance(expires_at, datetime)


def test_verify_rejects_expired_token():
    import jwt
    from auth import verify_token
    from settings import settings

    past = datetime.now(UTC) - timedelta(hours=1)
    token = jwt.encode(
        {"role": "admin", "exp": int(past.timestamp())},
        settings.dashboard_jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        verify_token(token)
    assert exc.value.status_code == 401


def test_verify_rejects_tampered_token():
    from auth import issue_token, verify_token

    token, _ = issue_token("admin")
    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(HTTPException) as exc:
        verify_token(tampered)
    assert exc.value.status_code == 401


def test_role_gate_viewer_denied_on_admin_route():
    from auth import _role_at_least

    assert _role_at_least("viewer", "admin") is False
    assert _role_at_least("admin", "admin") is True
    assert _role_at_least("admin", "viewer") is True
    assert _role_at_least("viewer", "viewer") is True


# ── Placeholder credentials (the `.env.example` hole) ────────────────────────
# `.env.example` used to ship working values (`change-me-admin`), and requiring
# the variable to be *set* did not help: it was set, to the published value. The
# deployed lxp node was found running both dashboard passwords at exactly those
# strings. `check_password` now refuses a match against a placeholder.


@pytest.mark.parametrize(
    "value",
    [
        "change-me-viewer",
        "change-me-admin",
        "CHANGE-ME-ADMIN",
        "changeme",
        "changeme123",
        "placeholder",
        "my-changeme-password",
        "replace-me-please",
        "your-token-here",
    ],
)
def test_is_placeholder_catches_decorated_examples(value):
    from auth import is_placeholder

    assert is_placeholder(value) is True


@pytest.mark.parametrize("value", ["test-viewer-pw", "test-admin-pw", "s3cr3t-r4nd0m-xyz"])
def test_is_placeholder_passes_real_secrets(value):
    from auth import is_placeholder

    assert is_placeholder(value) is False


def test_check_password_refuses_placeholder_admin(monkeypatch):
    import auth
    from settings import settings

    monkeypatch.setattr(settings, "dashboard_admin_password", "change-me-admin")
    assert auth.check_password("change-me-admin") is None
    # the other role is untouched by its neighbour's misconfiguration
    assert auth.check_password("test-viewer-pw") == "viewer"


def test_check_password_refuses_placeholder_viewer(monkeypatch):
    import auth
    from settings import settings

    monkeypatch.setattr(settings, "dashboard_viewer_password", "change-me-viewer")
    assert auth.check_password("change-me-viewer") is None
    assert auth.check_password("test-admin-pw") == "admin"
