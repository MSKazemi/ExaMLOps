"""Cross-tenant isolation conformance (enterprise-readiness Phase 2, item 2.6).

The security guarantee: a query scoped to one tenant must NEVER return another tenant's rows.

**Read what this covers before trusting it.** It proves the guarantee for the three stores named
below — secrets, SLO specs, policy bundles — by seeding two tenants and asserting each list is
single-tenant. It used to say it seeded "every tenant-scoped store"; there are **31** tables with a
`tenant` column, so that claim was wrong by an order of magnitude, and it was the kind of wrong that
stops anyone looking. It did stop someone looking: the dashboard's own tenant filter turned out to
have no callers at all, and `GET /api/slo` was returning every tenant's SLO definitions to every
viewer (`tests/unit/test_dashboard_tenant_scoping.py`, which ratchets that surface).

What this file covers is the **data layer** — the helpers that take a `tenant` argument. The layer
above it, where a route must decide *which* tenant to ask for, is the one that failed, and no
amount of conformance down here would have caught it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

A = "tenant-a"
B = "tenant-b"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for k in (
        "EXAMLOPS_SECRETS_KEYS",
        "EXAMLOPS_SECRETS_ACTIVE_KEY",
        "EXAMLOPS_SECRETS_KEY",
        "DASHBOARD_SECRET_KEY",
        "EXAMLOPS_VAULT_ADDR",
    ):
        monkeypatch.delenv(k, raising=False)
    from cryptography.fernet import Fernet

    monkeypatch.setenv("EXAMLOPS_SECRETS_KEYS", f"k:{Fernet.generate_key().decode()}")
    monkeypatch.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "k")
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_secrets_are_tenant_isolated(db):
    from examlops import secrets

    secrets.set_secret("shared/path", "A-value", tenant=A)
    secrets.set_secret("shared/path", "B-value", tenant=B)  # same path, different tenant

    # Same path resolves to the caller's OWN tenant value — never the other's.
    assert secrets.get_secret("shared/path", tenant=A) == "A-value"
    assert secrets.get_secret("shared/path", tenant=B) == "B-value"

    # A secret that exists only under B is not readable as A.
    secrets.set_secret("b-only", "top-secret", tenant=B)
    with pytest.raises(secrets.SecretNotFound):
        secrets.get_secret("b-only", tenant=A)

    # Listing scoped to A never leaks B's paths.
    a_paths = {r["path"] for r in db.list_secret_paths(A)}
    b_paths = {r["path"] for r in db.list_secret_paths(B)}
    assert "b-only" not in a_paths
    assert "b-only" in b_paths
    assert all(r["tenant"] == A for r in db.list_secret_paths(A))


def test_secret_ciphertext_lookup_is_tenant_scoped(db):
    from examlops import secrets

    secrets.set_secret("k", "A-value", tenant=A)
    # No row for (path=k, tenant=B) → returns None even though A has one at the same path.
    assert db.get_secret_record("k", B) is None
    assert db.get_secret_record("k", A) is not None


def test_slo_specs_are_tenant_isolated(db):
    db.upsert_slo_spec("JPCP", "latency", tenant=A, target=0.99)
    db.upsert_slo_spec("JPCP", "latency", tenant=B, target=0.90)

    a_specs = db.list_slo_specs(tenant=A)
    assert a_specs and all(s["tenant"] == A for s in a_specs)
    # get scoped to B sees B's target, A sees A's — same (model, name).
    assert db.get_slo_spec("JPCP", "latency", tenant=A)["target"] == 0.99
    assert db.get_slo_spec("JPCP", "latency", tenant=B)["target"] == 0.90
    # A spec that exists only for B is invisible to A.
    db.upsert_slo_spec("BONLY", "sli", tenant=B)
    assert db.get_slo_spec("BONLY", "sli", tenant=A) is None


def test_policy_bundles_are_tenant_isolated(db):
    db.store_policy_bundle(A, "package a", "hash-a")
    db.store_policy_bundle(B, "package b", "hash-b")

    assert db.get_policy_bundle(A)["content"] == "package a"
    assert db.get_policy_bundle(B)["content"] == "package b"
    assert all(r["tenant"] == A for r in db.list_policy_bundles(A))
    # A tenant with no bundle sees nothing (not another tenant's).
    assert db.get_policy_bundle("tenant-c") is None


def test_no_tenant_scoped_helper_returns_foreign_rows(db):
    """Aggregate guard: seed A+B and assert every tenant-scoped list is single-tenant."""
    from examlops import secrets

    secrets.set_secret("s", "va", tenant=A)
    secrets.set_secret("s", "vb", tenant=B)
    db.upsert_slo_spec("M", "sli", tenant=A)
    db.upsert_slo_spec("M", "sli", tenant=B)
    db.store_policy_bundle(A, "a", "ha")
    db.store_policy_bundle(B, "b", "hb")

    for lister in (
        lambda t: db.list_secret_paths(t),
        lambda t: db.list_slo_specs(tenant=t),
        lambda t: db.list_policy_bundles(t),
    ):
        for tenant in (A, B):
            rows = lister(tenant)
            assert rows, f"expected rows for {tenant}"
            assert all(r["tenant"] == tenant for r in rows), f"cross-tenant leak in {lister}"
