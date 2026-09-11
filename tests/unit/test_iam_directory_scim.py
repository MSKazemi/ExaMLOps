"""ADR 0132 — the federated account directory and SCIM 2.0 provisioning.

The property that matters most is asserted from the leaver's side: a token that is still valid must
stop working once the center deprovisions the account, and nothing (a fresh JIT sign-in, another
center, a reused username) may bring the access back.
"""

from __future__ import annotations

import time

import pytest
import yaml

from examlops import iam
from examlops.iam import config as iam_config
from examlops.iam import directory, scim
from examlops.iam.tokens import AuthenticationError, verify_access_token
from tests.unit._iam_fakes import FakeIdP

JSC_SCIM = "-".join(("jsc", "scim", "client", "credential"))
CIN_SCIM = "-".join(("cineca", "scim", "client", "credential"))
PATCH = scim.PATCH_SCHEMA


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_IAM_CONFIG", raising=False)
    monkeypatch.delenv("EXAMLOPS_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("JSC_SCIM", JSC_SCIM)
    monkeypatch.setenv("CIN_SCIM", CIN_SCIM)
    iam.clear_caches()
    yield
    iam.clear_caches()


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.stop()


def _cfg(tmp_path, monkeypatch, idp=None, *, mode="jit"):
    providers = [
        {
            "name": "jsc",
            "issuer": idp.issuer if idp else "https://login.jsc.example",
            "audience": "examlops",
            "tenant": "jsc",
            "default_role": "viewer",
            "provisioning": {"mode": mode, "token_ref": "env:JSC_SCIM"},
        },
        {
            "name": "cineca",
            "issuer": "https://login.cineca.example",
            "audience": "examlops",
            "tenant": "cineca",
            "provisioning": {"token_ref": "env:CIN_SCIM"},
        },
    ]
    path = tmp_path / "identity-providers.yaml"
    path.write_text(yaml.safe_dump({"providers": providers}))
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(path))
    iam.clear_caches()
    return iam.load_config()


def _principal(provider="jsc", subject="u-1", username="alice", email="alice@jsc.example"):
    return iam.Principal(
        provider=provider,
        issuer="https://i",
        subject=subject,
        tenant=provider,
        role="viewer",
        username=username,
        email=email,
    )


# ── configuration ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prov, needle",
    [
        ({"mode": "scim"}, "needs token_ref"),
        ({"mode": "strict"}, "is not one of"),
        ({"token_ref": "plain-literal"}, "env: or secret:"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_provisioning_config_is_validated(prov, needle):
    _, errs = iam_config.parse_config(
        {
            "providers": [
                {"name": "c", "issuer": "https://i.example", "audience": "a", "provisioning": prov}
            ]
        }
    )
    assert any(needle in e for e in errs), errs


# ── directory + enforcement ───────────────────────────────────────────────────


def test_first_sight_records_the_account_and_readonly_does_not():
    directory.check(_principal(subject="ro"), record=False)
    assert directory.find("jsc", subject="ro") is None
    directory.check(_principal(subject="u-1"))
    acc = directory.find("jsc", subject="u-1")
    assert acc is not None and acc.source == "jit" and acc.active and acc.username == "alice"


def test_deactivation_refuses_a_still_valid_identity_immediately():
    p = _principal()
    directory.check(p)
    directory.deactivate(directory.find("jsc", subject="u-1"), actor="ops", reason="left")
    with pytest.raises(directory.AccountDenied, match="deactivated"):
        directory.check(p)  # same process: invalidated at once, no TTL wait
    directory.activate(directory.find("jsc", subject="u-1"), actor="ops")
    directory.check(p)


def test_other_processes_see_a_deactivation_within_the_cache_ttl(monkeypatch):
    """Bounded staleness, asserted without wall-clock timing (a moved monotonic clock)."""
    monkeypatch.setenv("EXAMLOPS_IAM_ACCOUNT_CACHE_TTL", "30")
    p = _principal()
    directory.check(p)  # caches "allowed" for 30 s
    acc = directory.find("jsc", subject="u-1")
    # Simulate another process deactivating: write the row without touching this process's cache.
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("UPDATE iam_accounts SET active=0 WHERE id=?", (acc.id,))
    directory.check(p)  # inside the TTL: still the cached verdict
    real = time.monotonic
    monkeypatch.setattr(directory.time, "monotonic", lambda: real() + 31)
    with pytest.raises(directory.AccountDenied):
        directory.check(p)  # past the TTL: the other process's deactivation bites


def test_a_deleted_account_is_a_tombstone_that_jit_never_resurrects():
    p = _principal()
    directory.check(p)
    directory.delete(directory.find("jsc", subject="u-1"), actor="scim:jsc")
    for _ in range(2):
        with pytest.raises(directory.AccountDenied, match="removed"):
            directory.check(p)
    assert directory.list_accounts("jsc")[1] == 0  # gone from listings


def test_strict_scim_mode_refuses_unprovisioned_and_links_provisioned_on_first_sign_in():
    with pytest.raises(directory.AccountDenied, match="not been provisioned"):
        directory.check(_principal(subject="stranger", username="bob"), "scim")
    acc = directory.provision("jsc", username="alice", email="alice@jsc.example")
    assert acc.subject is None
    directory.check(_principal(subject="u-1", username="alice"), "scim")
    linked = directory.get(acc.id)
    assert linked.subject == "u-1" and linked.last_seen_at


def test_a_reused_username_neither_inherits_nor_takes_over_another_subject():
    directory.check(_principal(subject="old-person", username="alice"))
    directory.deactivate(directory.find("jsc", subject="old-person"), actor="ops")
    # A different person now holds the username at the center: not blocked by, nor linked to, the
    # old person's account.
    directory.check(_principal(subject="new-person", username="alice"))
    assert (
        directory.find("jsc", subject="new-person").id
        != directory.find("jsc", subject="old-person").id
    )
    assert not directory.find("jsc", subject="old-person").active


def test_datastore_errors_fail_closed_only_in_strict_mode(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(directory, "find", boom)
    directory.check(_principal(subject="x"), "jit")  # IdP stays the authority: allowed, logged
    with pytest.raises(directory.AccountDenied, match="unavailable"):
        directory.check(_principal(subject="y"), "scim")


def test_verify_access_token_refuses_a_deprovisioned_account(idp, tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, idp)
    token = idp.mint()
    assert verify_access_token(token).role == "viewer"
    directory.deactivate(directory.find("jsc", subject="u-123"), actor="ops")
    with pytest.raises(AuthenticationError, match="deactivated"):
        verify_access_token(token)  # the token itself is still perfectly valid
    # Inspection tools see the verdict without writing:
    with pytest.raises(AuthenticationError):
        verify_access_token(token, accounts="readonly")
    assert verify_access_token(token, accounts="off").subject == "u-123"


# ── SCIM protocol ─────────────────────────────────────────────────────────────

BASE = "https://dash.example/api/scim/v2"


def _user(name="alice", **extra):
    return {
        "schemas": [scim.USER_SCHEMA],
        "userName": name,
        "externalId": f"ext-{name}",
        "active": True,
        "name": {"givenName": "Alice", "familyName": "Liddell", "formatted": "Alice Liddell"},
        "emails": [{"value": f"{name}@jsc.example", "type": "work", "primary": True}],
        **extra,
    }


def test_scim_authentication_selects_and_confines_the_provider(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert scim.authenticate(f"Bearer {JSC_SCIM}", cfg).name == "jsc"
    assert scim.authenticate(f"Bearer {CIN_SCIM}", cfg).name == "cineca"
    for bad in (None, "Basic abc", "Bearer nope"):
        with pytest.raises(scim.ScimError) as ei:
            scim.authenticate(bad, cfg)
        assert ei.value.status == 401


def test_scim_user_lifecycle_entra_style(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    jsc = cfg.by_name("jsc")
    created = scim.create_user(jsc, BASE, _user(**{scim.ENTERPRISE_SCHEMA: {"department": "HPC"}}))
    uid = created["id"]
    assert created["active"] is True and created["meta"]["location"] == f"{BASE}/Users/{uid}"
    assert created[scim.ENTERPRISE_SCHEMA] == {"department": "HPC"}
    # Entra's "does this user exist?" probe
    found = scim.list_users(jsc, BASE, filter='userName eq "alice"', start_index=1, count=10)
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == uid
    # Entra's PATCH dialect: capitalised ops, string booleans, emails[type eq "work"].value
    patched = scim.patch_user(
        jsc,
        BASE,
        uid,
        {
            "schemas": [PATCH],
            "Operations": [
                {
                    "op": "Replace",
                    "path": 'emails[type eq "work"].value',
                    "value": "a.l@jsc.example",
                },
                {"op": "Replace", "path": "active", "value": "False"},
            ],
        },
    )
    assert patched["active"] is False and patched["emails"][0]["value"] == "a.l@jsc.example"
    acc = directory.get(uid)
    assert acc.email == "a.l@jsc.example" and acc.deactivated_by == "scim:jsc"


def test_scim_okta_style_value_object_patch_and_put(tmp_path, monkeypatch):
    jsc = _cfg(tmp_path, monkeypatch).by_name("jsc")
    uid = scim.create_user(jsc, BASE, _user("bob"))["id"]
    scim.patch_user(
        jsc,
        BASE,
        uid,
        {"schemas": [PATCH], "Operations": [{"op": "replace", "value": {"active": False}}]},
    )
    assert directory.get(uid).active is False
    replaced = scim.replace_user(jsc, BASE, uid, _user("bob", displayName="Robert", active=True))
    assert replaced["active"] is True and replaced["displayName"] == "Robert"


def test_scim_delete_tombstones_and_repost_revives(tmp_path, monkeypatch):
    jsc = _cfg(tmp_path, monkeypatch).by_name("jsc")
    uid = scim.create_user(jsc, BASE, _user())["id"]
    scim.delete_user(jsc, uid)
    with pytest.raises(scim.ScimError) as ei:
        scim.get_user(jsc, BASE, uid)
    assert ei.value.status == 404
    assert scim.list_users(jsc, BASE, filter=None, start_index=1, count=10)["totalResults"] == 0
    revived = scim.create_user(jsc, BASE, _user())
    assert revived["id"] == uid and revived["active"] is True  # the same account, not a twin


def test_one_center_cannot_see_or_touch_anothers_accounts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    uid = scim.create_user(cfg.by_name("jsc"), BASE, _user())["id"]
    cin = cfg.by_name("cineca")
    assert scim.list_users(cin, BASE, filter=None, start_index=1, count=10)["totalResults"] == 0
    for call in (
        lambda: scim.get_user(cin, BASE, uid),
        lambda: scim.patch_user(
            cin,
            BASE,
            uid,
            {
                "schemas": [PATCH],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
        ),
        lambda: scim.delete_user(cin, uid),
    ):
        with pytest.raises(scim.ScimError) as ei:
            call()
        assert ei.value.status == 404
    assert directory.get(uid).active is True


@pytest.mark.parametrize(
    "call, status, scim_type",
    [
        (lambda j: scim.create_user(j, BASE, _user()), 409, "uniqueness"),
        (lambda j: scim.create_user(j, BASE, {"schemas": [scim.USER_SCHEMA]}), 400, "invalidValue"),
        (
            lambda j: scim.list_users(j, BASE, filter='displayName co "x"', start_index=1, count=1),
            400,
            "invalidFilter",
        ),
    ],
)
def test_scim_errors_follow_rfc7644(tmp_path, monkeypatch, call, status, scim_type):
    jsc = _cfg(tmp_path, monkeypatch).by_name("jsc")
    scim.create_user(jsc, BASE, _user())
    with pytest.raises(scim.ScimError) as ei:
        call(jsc)
    assert ei.value.status == status and ei.value.scim_type == scim_type
    body = ei.value.body()
    assert body["schemas"] == [scim.ERROR_SCHEMA] and body["status"] == str(status)


def test_scim_paging(tmp_path, monkeypatch):
    jsc = _cfg(tmp_path, monkeypatch).by_name("jsc")
    for i in range(5):
        scim.create_user(jsc, BASE, _user(f"user{i}"))
    page = scim.list_users(jsc, BASE, filter=None, start_index=3, count=2)
    assert page["totalResults"] == 5 and page["startIndex"] == 3 and page["itemsPerPage"] == 2


def test_provisioned_then_signed_in_then_deprovisioned_end_to_end(idp, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, idp, mode="scim")
    jsc = cfg.by_name("jsc")
    token = idp.mint()  # preferred_username "alice", sub "u-123"
    with pytest.raises(AuthenticationError, match="not been provisioned"):
        verify_access_token(token)
    iam.clear_caches()
    uid = scim.create_user(jsc, BASE, _user("alice"))["id"]
    assert verify_access_token(token).subject == "u-123"
    assert directory.get(uid).subject == "u-123"  # linked on first sign-in
    scim.patch_user(
        jsc,
        BASE,
        uid,
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    with pytest.raises(AuthenticationError, match="deactivated"):
        verify_access_token(token)
