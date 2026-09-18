"""The platform datastore must be in exactly one backup tier — whichever engine holds it.

Under ``EXAMLOPS_DB_BACKEND=postgres`` the SQLite ``platform.db`` file is an empty leftover: every
helper writes to Postgres. A bundle that snapshots that file looks complete and restores nothing,
which is the worst failure a backup tool has. These tests pin the handover in both directions —
the postgres tier picks the datastore up, the sqlite tier deliberately puts it down — plus the two
things a real restore taught us: ``pg_restore`` needs ``-d``, and a failed restore must not be
reported as success.

No live server: the ``_run`` / ``shutil.which`` seams are monkeypatched throughout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

_DSN = "postgresql://exa_user:s3cr3t@db.example:15433/examlops"
# The CLI paths below never reach a server: `exa`'s root callback opens the datastore on every
# invocation and retries for ~30s when it is down, which is the CLI's behaviour, not this file's
# subject. The callback is stubbed and the DSN points at a closed local port.
_UNREACHABLE_DSN = "postgresql://exa_user:s3cr3t@127.0.0.1:1/examlops"


@pytest.fixture
def offline_cli(monkeypatch):
    """A CLI whose root callback does not open the datastore."""
    from examlops.cli import main as cli_main

    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", _UNREACHABLE_DSN)
    monkeypatch.setattr(cli_main, "_init_platform_db", lambda: None)
    return cli_main.app


@pytest.fixture
def pg_engine(monkeypatch):
    """Select the Postgres engine for the process under test."""
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", _DSN)
    monkeypatch.delenv("EXAMLOPS_POSTGRES_SCHEMA", raising=False)


def _record_runs(monkeypatch, module, rc=0, err=""):
    """Replace the subprocess seam and collect every (cmd, env) it was called with."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, env):
        calls.append((list(cmd), dict(env)))
        out = [a for a in cmd if a.endswith(".dump")]
        if rc == 0 and out and cmd[0] == "pg_dump":
            Path(out[0]).write_bytes(b"PGDMP-fake")
        return rc, err

    monkeypatch.setattr(module, "_run", fake_run)
    return calls


# ── which tier owns platform state ─────────────────────────────────────────────


def test_platform_dsn_is_none_on_sqlite(monkeypatch):
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", _DSN)
    assert postgres_tier.platform_dsn() is None


def test_platform_dsn_is_none_when_dsn_unset(monkeypatch):
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    assert postgres_tier.platform_dsn() is None


def test_platform_dsn_reads_the_configured_engine(pg_engine):
    from examlops.backup import postgres_tier

    assert postgres_tier.platform_dsn() == _DSN


def test_sqlite_tier_skips_platform_under_postgres(tmp_path, pg_engine):
    from examlops.backup import sqlite_tier
    from examlops.backup._manifest import SKIPPED

    res = sqlite_tier.backup_sqlite_tier(tmp_path)
    platform = [i for i in res.items if i["name"] == "platform"]
    assert platform, "the platform entry must still be reported, not silently dropped"
    assert platform[0]["status"] == SKIPPED
    assert "postgres tier" in platform[0]["reason"]
    assert not list((tmp_path / "sqlite").glob("platform*.db"))


def test_postgres_tier_dumps_the_platform_datastore(tmp_path, pg_engine, monkeypatch):
    from examlops.backup import postgres_tier
    from examlops.backup._manifest import OK

    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")
    calls = _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.backup_postgres_tier(tmp_path)

    platform = [i for i in res.items if i["name"] == "platform"]
    assert len(platform) == 1
    assert platform[0]["status"] == OK
    assert platform[0]["file"] == "postgres/platform.dump"
    assert platform[0]["dsn_env"] == "EXAMLOPS_POSTGRES_DSN"
    assert (tmp_path / "postgres" / "platform.dump").exists()
    # …and it is a *separate* dump from the MLflow/Prefect ones.
    assert {i["name"] for i in res.items} >= {"mlflow", "prefect", "platform"}
    assert sum(1 for cmd, _ in calls if cmd[0] == "pg_dump") == 3


def test_postgres_tier_ignores_the_platform_db_on_sqlite(tmp_path, monkeypatch):
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")
    _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.backup_postgres_tier(tmp_path)
    assert [i["name"] for i in res.items] == ["mlflow", "prefect"]


# ── credentials and multi-instance scoping ─────────────────────────────────────


def test_password_never_reaches_argv(tmp_path, pg_engine, monkeypatch):
    """A DSN on the command line is readable by every user on the box via ``ps``."""
    from examlops.backup import postgres_tier

    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")
    calls = _record_runs(monkeypatch, postgres_tier)
    postgres_tier.backup_postgres_tier(tmp_path)

    platform_cmd, env = next(c for c in calls if "platform.dump" in " ".join(c[0]))
    assert not any("s3cr3t" in a for a in platform_cmd)
    assert not any(a.startswith("postgresql://") for a in platform_cmd)
    assert env["PGPASSWORD"] == "s3cr3t"
    assert env["PGHOST"] == "db.example"
    assert env["PGPORT"] == "15433"
    assert env["PGUSER"] == "exa_user"
    assert env["PGDATABASE"] == "examlops"


def test_dsn_env_url_decodes_credentials(pg_engine):
    from examlops.backup import postgres_tier

    env = postgres_tier._dsn_env("postgresql://a%40b:p%40ss%2Fw@h:5432/d")
    assert env["PGUSER"] == "a@b"
    assert env["PGPASSWORD"] == "p@ss/w"


def test_dsn_env_drops_a_stale_service_file(pg_engine, monkeypatch):
    monkeypatch.setenv("PGSERVICE", "some-other-cluster")
    from examlops.backup import postgres_tier

    assert "PGSERVICE" not in postgres_tier._dsn_env(_DSN)


def test_dump_is_scoped_to_the_configured_schema(tmp_path, pg_engine, monkeypatch):
    """One database can hold several platform instances — a bundle must carry only its own."""
    from examlops.backup import postgres_tier

    monkeypatch.setenv("EXAMLOPS_POSTGRES_SCHEMA", "tenant_a")
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")
    calls = _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.backup_postgres_tier(tmp_path)

    platform_cmd, _ = next(c for c in calls if "platform.dump" in " ".join(c[0]))
    assert "--schema" in platform_cmd
    assert platform_cmd[platform_cmd.index("--schema") + 1] == "tenant_a"
    assert next(i for i in res.items if i["name"] == "platform")["schema"] == "tenant_a"


def test_platform_dump_failure_is_reported_not_swallowed(tmp_path, pg_engine, monkeypatch):
    from examlops.backup import postgres_tier
    from examlops.backup._manifest import FAILED

    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_dump")

    def fake_run(cmd, env):
        if "platform.dump" in " ".join(cmd):
            return 1, 'pg_dump: error: schema "tenant_a" does not exist'
        Path([a for a in cmd if a.endswith(".dump")][0]).write_bytes(b"PGDMP-fake")
        return 0, ""

    monkeypatch.setattr(postgres_tier, "_run", fake_run)
    res = postgres_tier.backup_postgres_tier(tmp_path)
    platform = next(i for i in res.items if i["name"] == "platform")
    assert platform["status"] == FAILED
    assert "does not exist" in platform["reason"]


# ── restore ────────────────────────────────────────────────────────────────────


def _bundle_with_platform_dump(tmp_path, *, schema=None):
    pg_dir = tmp_path / "postgres"
    pg_dir.mkdir(parents=True, exist_ok=True)
    (pg_dir / "platform.dump").write_bytes(b"PGDMP-fake")
    manifest = {
        "tiers": {
            "postgres": {
                "items": [
                    {
                        "name": "platform",
                        "file": "postgres/platform.dump",
                        "dsn_env": "EXAMLOPS_POSTGRES_DSN",
                        "schema": schema,
                        "status": "ok",
                    }
                ]
            }
        }
    }
    (tmp_path / "bundle.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_restore_names_the_target_database_in_argv(tmp_path, pg_engine, monkeypatch):
    """``pg_restore`` refuses to take its target from ``PGDATABASE`` — it needs ``-d``."""
    from examlops.backup import postgres_tier

    bundle = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    calls = _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.restore_postgres_tier(bundle, force=True)

    assert res == [{"name": "platform", "ok": True, "reason": None}]
    cmd, env = calls[0]
    assert cmd[0] == "pg_restore"
    assert "-d" in cmd and cmd[cmd.index("-d") + 1] == "examlops"
    assert env["PGPASSWORD"] == "s3cr3t"
    assert not any("s3cr3t" in a for a in cmd)


def test_restore_refuses_when_the_engine_is_not_configured(tmp_path, monkeypatch):
    """Restoring platform state into a SQLite-configured process would be a silent no-op."""
    from examlops.backup import postgres_tier

    bundle = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.restore_postgres_tier(bundle, force=True)
    assert res[0]["ok"] is False
    assert "EXAMLOPS_POSTGRES_DSN" in res[0]["reason"]


def test_restore_refuses_a_dsn_without_a_database(tmp_path, monkeypatch):
    from examlops.backup import postgres_tier

    bundle = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", "postgresql://u:p@h:5432/")
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier)
    res = postgres_tier.restore_postgres_tier(bundle, force=True)
    assert res[0]["ok"] is False
    assert "no database" in res[0]["reason"]


def test_restore_failure_surfaces_in_the_bundle_result(tmp_path, pg_engine, monkeypatch):
    """A restore that failed but reported success is worse than one that raised."""
    from examlops.backup import bundle as bundle_mod
    from examlops.backup import postgres_tier

    bundle_dir = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier, rc=1, err="pg_restore: error: could not execute")

    result = bundle_mod.restore_bundle(bundle_dir, tiers=["postgres"], force=True)
    assert result["ok"] is False
    assert result["failed"] and result["failed"][0]["tier"] == "postgres"
    assert result["failed"][0]["name"] == "platform"


def test_successful_restore_reports_ok(tmp_path, pg_engine, monkeypatch):
    from examlops.backup import bundle as bundle_mod
    from examlops.backup import postgres_tier

    bundle_dir = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier)

    result = bundle_mod.restore_bundle(bundle_dir, tiers=["postgres"], force=True)
    assert result["ok"] is True
    assert result["failed"] == []


def test_a_postgres_restore_forgets_the_schema_is_ready_verdict(tmp_path, pg_engine, monkeypatch):
    """The restored schema may predate this process's cached "already initialised" answer.

    `restore_bundle` cleared that cache after the **sqlite** tier, with a comment explaining why —
    and not after the postgres tier, although exactly one of the two holds platform state at a
    time and which one is decided by the engine. So under the Postgres engine the clearing ran on
    the tier that was empty and was skipped on the tier that had just been replaced: `init_db()`
    went on answering "ready", skipping the additive DDL, the data-format stamp and every online
    migration, for a schema it had never actually looked at.

    Found by a live DR drill (`tests/integration/test_postgres_dr_roundtrip_live.py`) that dropped
    the schema out from under a running process.
    """
    from examlops.backup import bundle as bundle_mod
    from examlops.backup import postgres_tier
    from examlops.platform_db import _INITIALIZED_PATHS

    bundle_dir = _bundle_with_platform_dump(tmp_path)
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier)

    _INITIALIZED_PATHS.add("a schema this process believes it has already prepared")
    bundle_mod.restore_bundle(bundle_dir, tiers=["postgres"], force=True)

    assert not _INITIALIZED_PATHS, (
        "after restoring platform state the process still believes the schema is prepared, so the "
        "next helper will skip the DDL, the stamp and the migrations the restored data may need"
    )


# ── the CLI must not let an operator take a hollow backup ───────────────────────


def test_cli_create_refuses_the_sqlite_only_shape_under_postgres(
    tmp_path, pg_engine, offline_cli, monkeypatch
):
    """Bare ``exa backup create`` snapshots SQLite files only — under Postgres that is empty."""
    from typer.testing import CliRunner

    monkeypatch.setenv("EXAMLOPS_BACKUP_DIR", str(tmp_path / "auto"))
    res = CliRunner().invoke(offline_cli, ["backup", "create", "--out", str(tmp_path / "bk")])
    assert res.exit_code == 1
    assert "--with-postgres" in res.output
    assert not (tmp_path / "bk").exists()


def test_cli_restore_bundle_exits_nonzero_when_a_tier_failed(
    tmp_path, pg_engine, offline_cli, monkeypatch
):
    """The bug a live run caught: '✓ Restored tiers' printed over a failed restore."""
    from typer.testing import CliRunner

    from examlops import backup
    from examlops.backup import postgres_tier
    from examlops.cli.commands import backup_cmd

    bundle = _bundle_with_platform_dump(tmp_path / "bundle")
    # The pre-restore snapshot and the audit write both open the datastore; neither is what this
    # test is about, and both wait out the retry budget against a server that isn't there.
    monkeypatch.setattr(backup, "auto_backup_before", lambda *_a, **_k: None)
    monkeypatch.setattr(backup_cmd, "_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(postgres_tier.shutil, "which", lambda _: "/usr/bin/pg_restore")
    _record_runs(monkeypatch, postgres_tier, rc=1, err="pg_restore: error: could not execute")

    res = CliRunner().invoke(
        offline_cli,
        ["backup", "restore-bundle", str(bundle), "--tier", "postgres", "--force", "--yes"],
    )
    assert res.exit_code == 1
    assert "NOT back" in res.output
