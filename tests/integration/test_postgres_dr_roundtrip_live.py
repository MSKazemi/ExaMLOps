"""A backup nobody has restored is a hope, not a backup — so restore one, on the real engine.

`tests/unit/test_backup_restore.py` already does the whole disaster-recovery round trip: seed,
snapshot, delete the live database, restore, and assert every row and the audit chain came back. It
does it on **SQLite**.

The engine an enterprise deployment actually runs is Postgres, and there a backup is a `pg_dump`
custom-format archive put back by `pg_restore` — a different tier, a different code path, a
different failure surface. Everything covering it was unit-level: the argv is right, the password
never reaches `ps`, the dump is scoped to the configured schema, a failure is reported rather than
swallowed. All true, and none of it says the data comes back.

This drill says it. It seeds a chained audit log and a traffic split, takes a bundle, **drops the
schema** — the state the platform is in when the server it was using is gone, and the state every
unit test in this area stops short of creating — restores, and then asserts not merely that rows
exist but that the chain head hash is the one from before. Anything less would pass over a restore
that silently rewrote the log it is supposed to make tamper-evident.

It found one defect on its first run: `restore_bundle` cleared the cached "this schema is already
initialised" verdict after the *sqlite* tier and not after the *postgres* tier, so under the
Postgres engine the clearing ran on the tier that was empty and was skipped on the tier that had
just been replaced (`tests/unit/test_backup_platform_datastore.py`, same name as this file's
lesson).

**Running it.** Opt in with `EXAMLOPS_CHAOS_LIVE=1`; the drill starts and removes its own Postgres.
It needs `pg_dump`/`pg_restore` on `PATH` and skips without them, because a DR drill that silently
did not run is worse than no DR drill. `make chaos-drills` runs it with the rest of the set.

    EXAMLOPS_CHAOS_LIVE=1 .venv/bin/pytest tests/integration/test_postgres_dr_roundtrip_live.py -s

Point it at a server you already have with `EXAMLOPS_POSTGRES_DR_DSN=postgresql://u:p@host:port/db`
(then no container is started), which is also how to run it on a host whose `pg_dump` is a wrapper.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_CHAOS_LIVE") != "1", reason="set EXAMLOPS_CHAOS_LIVE=1"),
    pytest.mark.skipif(
        not (shutil.which("pg_dump") and shutil.which("pg_restore")),
        reason="needs pg_dump/pg_restore on PATH (install postgresql-client)",
    ),
]

IMAGE = "postgres:16-alpine"
USER = PASSWORD = DATABASE = "drtest"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server() -> str:
    """A Postgres to lose. Uses `EXAMLOPS_POSTGRES_DR_DSN` when set, else starts a throwaway one."""
    existing = os.getenv("EXAMLOPS_POSTGRES_DR_DSN")
    if existing:
        yield existing
        return

    if shutil.which("docker") is None:
        pytest.skip("needs docker to start a Postgres, or EXAMLOPS_POSTGRES_DR_DSN")
    name = f"exa-dr-pg-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-e", f"POSTGRES_DB={DATABASE}", "-p", f"127.0.0.1:{port}:5432", IMAGE,
        ],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", USER, "-d", DATABASE],
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"{IMAGE} never became ready")
        yield f"postgresql://{USER}:{PASSWORD}@127.0.0.1:{port}/{DATABASE}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def platform(server, monkeypatch, tmp_path) -> str:
    """Point the platform at a throwaway schema on that server, and drop it afterwards."""
    schema = f"dr_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", server)
    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", schema)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "unused-under-postgres.db"))
    # The tier also dumps the MLflow and Prefect metadata databases. They do not exist on a
    # throwaway server, and whether they dump is not what this drill is about.
    monkeypatch.setenv("EXAMLOPS_BACKUP_PG_DBS", "")

    from examlops.platform_db import init_db

    init_db()
    yield schema

    import psycopg

    with psycopg.connect(server) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()


def _seed() -> dict:
    from examlops.data.audit import verify_audit_chain, write_audit_event
    from examlops.platform_db import set_traffic_rules

    for i in range(5):
        write_audit_event("exa", "operator", "promote", f"JPCP-{i}")
    set_traffic_rules("JPCP", {"Production": 90, "Canary": 10})
    chain = verify_audit_chain()
    assert chain["ok"] and chain["count"] == 5, chain
    return chain


def test_a_postgres_backup_can_actually_be_restored(platform, server, tmp_path):
    """Seed → bundle → drop the schema → restore → the rows and the chain head are back."""
    import psycopg

    from examlops.backup.bundle import create_bundle, restore_bundle, verify_bundle

    before = _seed()

    result = create_bundle(str(tmp_path / "backups"), tiers=["postgres"])
    tier = result.manifest.get("tiers", {}).get("postgres", {})
    items = {i["name"]: i.get("status") for i in tier.get("items", [])}
    print("\npostgres tier:", tier.get("status"), json.dumps(items))
    assert items.get("platform") == "ok", (
        f"the platform datastore was not dumped, so the bundle is hollow: {tier}"
    )
    assert verify_bundle(str(result.bundle_dir))["ok"], "the bundle does not verify before restore"

    # Catastrophic loss, the Postgres way.
    with psycopg.connect(server) as conn:
        conn.execute(f'DROP SCHEMA "{platform}" CASCADE')
        conn.commit()

    restored = restore_bundle(str(result.bundle_dir), tiers=["postgres"], force=True)
    detail = restored.get("detail", {}).get("postgres") or []
    print("restore:", json.dumps(detail))
    assert not restored.get("failed"), restored["failed"]
    assert [i["name"] for i in detail if i.get("ok")] == ["platform"], detail

    from examlops.data.audit import verify_audit_chain
    from examlops.platform_db import get_traffic_rules

    after = verify_audit_chain()
    assert after["ok"] is True, f"the audit chain does not verify after a restore: {after}"
    assert after["count"] == before["count"], "events went missing across the round trip"
    assert after["head_hash"] == before["head_hash"], (
        "the chain head changed across a backup and restore — the log is not byte-identical, and "
        "byte-identical is the whole of what tamper-evidence rests on"
    )
    assert get_traffic_rules("JPCP") == {"Production": 90, "Canary": 10}
    print(f"recovered {after['count']} chained events, head {after['head_hash'][:12]}…")


def test_restoring_over_a_live_schema_needs_force(platform, tmp_path):
    """The guard that stops a restore from quietly overwriting a working deployment."""
    from examlops.backup.bundle import create_bundle, restore_bundle

    _seed()
    result = create_bundle(str(tmp_path / "backups"), tiers=["postgres"])

    with pytest.raises(Exception) as excinfo:
        restore_bundle(str(result.bundle_dir), tiers=["postgres"], force=False)
    assert "force" in str(excinfo.value).lower(), excinfo.value
