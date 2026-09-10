"""Capability model + tenant scoping (F15 / ADR 0057)."""

import capabilities as cap
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW

# ── capability catalogue (F15 R2/R3) ─────────────────────────────────────────


def test_viewer_has_only_read_capabilities():
    caps = set(cap.capabilities_for("viewer"))
    # `cli.run` is a read capability: it runs only `read`-tier `exa` commands (ADR 0119).
    assert caps == {cap.VIEW, cap.SEARCH, cap.CLI_RUN}
    assert not cap.can("viewer", cap.CLI_WRITE)
    assert not cap.can("viewer", cap.MODEL_PROMOTE)


def test_admin_has_governed_capabilities():
    assert cap.can("admin", cap.MODEL_PROMOTE)
    assert cap.can("admin", cap.APPROVAL_DECIDE)
    assert cap.can("admin", cap.SECRET_REVEAL)


def test_unknown_role_denied_by_default():
    assert cap.capabilities_for("ghost") == []
    assert not cap.can("ghost", cap.VIEW)


def test_deny_reason_explains():
    assert cap.deny_reason("viewer", cap.MODEL_PROMOTE) == "Requires the admin role."
    assert cap.deny_reason("admin", cap.MODEL_PROMOTE) == ""  # allowed → no reason


def test_step_up_flagged_for_governed_actions():
    assert cap.requires_step_up(cap.MODEL_PROMOTE)
    assert cap.requires_step_up(cap.SECRET_REVEAL)
    assert not cap.requires_step_up(cap.SEARCH)


# ── principal ────────────────────────────────────────────────────────────────


def test_principal_defaults_tenant_and_lists_caps():
    p = cap.principal_from_claims({"role": "viewer"})
    assert p["tenant"] == "default"
    assert p["role"] == "viewer"
    assert cap.SEARCH in p["capabilities"]


# ── tenant scoping (F15 R4, default-deny) ────────────────────────────────────


def test_same_tenant_visible_cross_tenant_denied_for_viewer():
    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    assert cap.tenant_visible(p, "alpha")
    assert not cap.tenant_visible(p, "beta")


def test_admin_sees_cross_tenant():
    p = cap.principal_from_claims({"role": "admin", "tenant": "alpha"})
    assert cap.tenant_visible(p, "beta")


def test_scope_to_tenant_filters_rows():
    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    rows = [{"tenant": "alpha"}, {"tenant": "beta"}, {"id": 3}]  # 3rd is 'default'
    scoped = cap.scope_to_tenant(p, rows)
    assert scoped == [{"tenant": "alpha"}]


def test_assert_tenant_access_raises_403():
    from fastapi import HTTPException

    p = cap.principal_from_claims({"role": "viewer", "tenant": "alpha"})
    with pytest.raises(HTTPException) as ei:
        cap.assert_tenant_access(p, "beta")
    assert ei.value.status_code == 403


# ── /me capability list (F15) ────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_me_returns_capabilities_for_viewer(client):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "default"
    assert set(body["capabilities"]) == {cap.VIEW, cap.SEARCH, cap.CLI_RUN}


@pytest.mark.asyncio
async def test_me_returns_governed_capabilities_for_admin(client):
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert cap.MODEL_PROMOTE in r.json()["capabilities"]


# ── require_capability dependency (F15 R2) ───────────────────────────────────


def test_require_capability_dependency():
    """The guard dependency rejects a role lacking the capability (403) and passes one that has it."""
    from fastapi import HTTPException

    guard = cap.require_capability(cap.MODEL_PROMOTE)
    # the returned dependency takes the verified claims; call it directly
    with pytest.raises(HTTPException) as ei:
        guard(claims={"role": "viewer"})
    assert ei.value.status_code == 403

    assert guard(claims={"role": "admin"}) == {"role": "admin"}


# ── the capability model spans two languages; keep the halves from drifting ───
#
# The UI and the BFF each hold their own copy of the step-up designation and of the capability
# names. Nothing links them at build time, so a change on one side is silent on the other — and
# the two halves disagreeing about *which* actions are high-risk is exactly the kind of
# divergence nobody notices until it matters.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

_FRONTEND_CAPS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "capabilities.ts"
)


def _ts_source() -> str:
    return _FRONTEND_CAPS.read_text()


def _ts_step_up() -> set[str]:
    """The capability values in the frontend's `STEP_UP` set."""
    src = _ts_source()
    m = re.search(r"const STEP_UP[^=]*=\s*new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "could not find the frontend STEP_UP set — the guard needs updating"
    names = re.findall(r"CAP\.([A-Z_]+)", m.group(1))
    catalogue = _ts_cap_catalogue()
    return {catalogue[n] for n in names}


def _ts_cap_catalogue() -> dict[str, str]:
    """The frontend's `CAP` object as {NAME: 'value'}."""
    src = _ts_source()
    m = re.search(r"export const CAP = \{(.*?)\} as const", src, re.S)
    assert m, "could not find the frontend CAP catalogue"
    return dict(re.findall(r"([A-Z_]+):\s*'([^']+)'", m.group(1)))


@pytest.mark.skipif(not _FRONTEND_CAPS.is_file(), reason="frontend not present")
def test_step_up_designation_matches_across_the_language_boundary():
    assert _ts_step_up() == set(cap.STEP_UP_CAPABILITIES), (
        "the UI and the BFF disagree about which actions are designated step-up/MFA"
    )


@pytest.mark.skipif(not _FRONTEND_CAPS.is_file(), reason="frontend not present")
def test_every_frontend_capability_exists_in_the_backend():
    """A capability the UI names but the BFF has never heard of is a permanently dead control.

    `deny_reason` would explain it as a role problem — "Your role ('admin') does not permit
    'secrets.write'" — which sends the reader after the wrong thing entirely.
    """
    backend = set(cap.capabilities_for("admin")) | set(cap.capabilities_for("viewer"))
    unknown = sorted(v for v in _ts_cap_catalogue().values() if v not in backend)
    assert not unknown, f"frontend names capabilities the backend does not define: {unknown}"


@pytest.mark.skipif(not _FRONTEND_CAPS.is_file(), reason="frontend not present")
def test_step_up_is_not_described_as_enforced_while_nothing_enforces_it():
    """Both directions: no "required/enforced" wording without a caller, and vice versa.

    `requires_step_up` has no request-path caller and `requiresStepUp` has no component caller, so
    the designation is a placeholder. Saying otherwise tells a reader a promote is protected by a
    second factor when it is not.
    """
    backend_dir = Path(__file__).resolve().parents[1]
    callers = [
        p
        for p in backend_dir.rglob("*.py")
        if "tests" not in p.parts
        and p.name != "capabilities.py"
        and "requires_step_up" in p.read_text()
    ]
    ts = _ts_source()
    claims_enforcement = "before the BFF permits" in ts or "require step-up" in ts

    if not callers:
        assert not claims_enforcement, (
            "the frontend describes step-up as enforced by the BFF, but no request path calls "
            "requires_step_up. Describe it as *designated*, not required."
        )
    else:
        assert claims_enforcement, (
            f"{[p.name for p in callers]} enforce step-up now — say so in the frontend comment "
            "instead of calling it a placeholder."
        )
