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
