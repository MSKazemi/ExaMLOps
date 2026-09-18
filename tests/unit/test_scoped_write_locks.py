"""Scoped write locks: each domain serialises only itself (plan P1.3 / finding P1).

On Postgres every ``BEGIN IMMEDIATE`` became one advisory lock, ``examlops-platform-write``, so an
audit append, an outbox claim, an admission claim and an approval transition anywhere in the
cluster waited for each other. The scoped form keeps the old behaviour for unscoped callers and
lets a domain name its own lock. The live-server proof is in
``tests/integration/test_postgres_backend_live.py``; this file pins the translation and the call
sites, so a refactor cannot quietly put a domain back on the global lock — or, worse, split one
invariant across two scopes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from examlops.platform_db import begin_immediate
from examlops.storage.pg import GLOBAL_WRITE_LOCK, lock_key, translate

ROOT = Path(__file__).resolve().parents[2]


def test_unscoped_keeps_the_global_lock():
    assert translate("BEGIN IMMEDIATE") == (
        f"SELECT pg_advisory_xact_lock(hashtext('{GLOBAL_WRITE_LOCK}'))"
    )


@pytest.mark.parametrize("scope", ["audit", "outbox", "admission", "coordination", "approvals"])
def test_a_scope_gets_its_own_key(scope):
    assert translate(begin_immediate(scope)) == (
        f"SELECT pg_advisory_xact_lock(hashtext('{GLOBAL_WRITE_LOCK}:{scope}'))"
    )
    assert lock_key(scope) != lock_key(None)


@pytest.mark.parametrize("bad", ["", "Audit", "a b", "x;DROP", "../x", "a" * 64])
def test_invalid_scopes_are_refused(bad):
    with pytest.raises(ValueError):
        begin_immediate(bad)


def test_sqlite_accepts_the_scoped_form():
    from examlops.resilience import db as _rdb

    conn = _rdb.connect(":memory:")
    conn.isolation_level = None
    conn.execute(begin_immediate("audit"))
    assert conn.in_transaction
    conn.execute("COMMIT")


def _scopes_in(path: str) -> set[str | None]:
    source = (ROOT / path).read_text(encoding="utf-8")
    scoped = set(re.findall(r'(?:_immediate_write|begin_immediate)\("([a-z0-9_.-]+)"\)', source))
    bare = re.search(r'_immediate_write\(\)|execute\("BEGIN IMMEDIATE"\)', source)
    return scoped | ({None} if bare else set())


def test_every_audit_chain_append_takes_the_audit_scope():
    """The hash chain's integrity rests on ONE lock across all appenders."""
    assert _scopes_in("platform/cli/src/examlops/data/audit.py") == {"audit"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("platform/cli/src/examlops/data/events.py", {"outbox"}),
        ("platform/cli/src/examlops/data/admission.py", {"admission"}),
        ("platform/cli/src/examlops/data/coordination.py", {"coordination"}),
        (
            "platform/services/control_plane/app.py",
            # `settings`: the shared runtime settings a PUT /v1/modelzoo/config writes (plan P5.1).
            {"admission", "approvals", "modelzoo", "schema", "settings"},
        ),
        # The schema bootstrap: replicas booting together on an empty Postgres serialise on it.
        ("platform/cli/src/examlops/platform_db.py", {"schema"}),
    ],
)
def test_domains_use_their_scopes(path, expected):
    assert _scopes_in(path) == expected
