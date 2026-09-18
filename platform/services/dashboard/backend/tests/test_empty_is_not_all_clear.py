"""A failed read must not render as an all-clear.

Six governance and risk reads answered a broken datastore with `[]` — no log, no signal — and the
console draws an empty list exactly the way it draws "nothing to report". On these six surfaces
"nothing to report" is a *claim*: no models are in scope of the EU AI Act, no model is drifting, no
SLO is defined, no fairness policy applies. A missing table, a datastore the process cannot reach,
or a `NameError` introduced by an edit therefore produced a reassuring answer rather than an error.

Each test points the router at an empty (schema-less) database — the shape of a failed or partial
migration — and asserts the route refuses rather than inventing a clean register. The routers
already raise `503` when their *import* is unavailable; these make a failed *query* answer the same
way, which the frontend renders as an error with retry.
"""

import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def broken_db(tmp_path, monkeypatch):
    """A datastore that exists but holds none of the tables — a half-applied migration."""
    db = tmp_path / "no-schema.db"
    db.write_bytes(b"")
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _viewer_headers(client):
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.mark.parametrize(
    ("path", "claim"),
    [
        ("/api/compliance/systems", "no model is in scope of the EU AI Act"),
        ("/api/fairness", "no fairness policy is configured"),
        ("/api/slo", "no SLO is defined"),
        ("/api/drift/status", "no model is drifting"),
        ("/api/drift/auto-retrain", "no model auto-retrains"),
        ("/api/drift/input-status", "no input distribution has moved"),
    ],
)
async def test_a_failed_read_is_not_an_empty_register(client, broken_db, path, claim):
    r = await client.get(path, headers=await _viewer_headers(client))
    assert r.status_code == 503, (
        f"{path} answered {r.status_code} with {r.text!r} when the datastore is unreadable. "
        f"An empty body here reads as: {claim}."
    )
    assert "unavailable" in r.text.lower()


async def test_the_healthy_path_still_answers_empty_as_empty(client, tmp_path, monkeypatch):
    """The refusal must be caused by the failure, not by the route being empty.

    With a real schema and no rows, every one of these is a legitimate empty register and must
    still answer `200 []` — otherwise the guard above would pass for the wrong reason.
    """
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db()

    headers = await _viewer_headers(client)
    for path in (
        "/api/compliance/systems",
        "/api/fairness",
        "/api/slo",
        "/api/drift/status",
        "/api/drift/auto-retrain",
        "/api/drift/input-status",
    ):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text!r} on an empty schema"
        assert r.json() == [], f"{path} invented rows on an empty schema: {r.text!r}"
