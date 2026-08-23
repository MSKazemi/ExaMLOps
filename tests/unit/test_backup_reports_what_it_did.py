"""`exa backup create` must not announce a backup that did not happen.

The command printed a green tick and exited 0 for every outcome — ``status=partial`` and
``status=failed`` included — while the per-tier lines that said *why* went through ``info()``,
which ``--quiet`` suppresses and the tick survives. The worst reachable version of that is an
operator running ``exa backup create --quiet --all`` on a host where the object store cannot be
reached, being told the backup succeeded, and discovering at restore time that it contains no
model artifacts at all.

The endpoint test is here rather than with the object tier because it is the same failure: the
default was a port this project uses nowhere, so the tier could only ever skip, and skipping was
reported as success.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.backup import _manifest, bundle  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()

sqlite_tier_only = pytest.mark.skipif(
    os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() == "postgres",
    reason="the SQLite tier has no platform.db to snapshot under the Postgres backend",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_BACKUP_DIR", str(tmp_path / "auto"))
    # The config tier tars the operator's ~/.config/examlops; point it somewhere hermetic so the
    # bundle's contents do not depend on whose laptop this is.
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    (tmp_path / "cfg").mkdir()
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


@sqlite_tier_only
def test_a_skipped_tier_is_not_reported_as_a_successful_backup(db, tmp_path):
    """Postgres cannot run off-stack, so this bundle is missing a tier that was asked for."""
    result = runner.invoke(
        app, ["backup", "create", "--out", str(tmp_path / "bk"), "--with-postgres"]
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "incomplete" in out, "a partial bundle must not read as a plain success"
    assert "postgres" in out and "skipped" in out


@sqlite_tier_only
def test_quiet_still_says_which_tier_produced_nothing(db, tmp_path):
    """`--quiet` may hide detail; it may not turn an incomplete backup into a silent success."""
    result = runner.invoke(
        app, ["--quiet", "backup", "create", "--out", str(tmp_path / "bk"), "--with-postgres"]
    )

    assert result.exit_code == 0, result.output
    assert "postgres" in result.output, "the skipped tier vanished under --quiet"
    assert "incomplete" in result.output


@sqlite_tier_only
def test_a_failed_tier_exits_nonzero(db, tmp_path, monkeypatch):
    """A tier that raises something other than TierUnavailable is a failure, not a degrade."""

    def boom(*_a, **_k):
        raise RuntimeError("disk went away mid-dump")

    monkeypatch.setattr(bundle.postgres_tier, "backup_postgres_tier", boom)

    result = runner.invoke(
        app, ["backup", "create", "--out", str(tmp_path / "bk"), "--with-postgres"]
    )

    assert result.exit_code != 0, result.output
    assert "failed" in result.output
    assert "disk went away" in result.output, "the operator must be told what broke"


def test_the_status_vocabulary_this_guard_relies_on_still_exists():
    """Guards the guard: renaming a status constant would make the assertions above vacuous."""
    assert (_manifest.OK, _manifest.SKIPPED, _manifest.FAILED) == ("ok", "skipped", "failed")
    assert _manifest.PARTIAL == "partial"


def test_the_object_store_default_points_at_a_port_this_project_uses():
    """Both call sites defaulted to ``localhost:9000`` — the host publishes 19000 and the compose
    network uses ``minio:9000``, so the fallback was correct in neither place. It only ever showed
    up as an object tier that could not connect, which the command then called a success."""
    from examlops.backup import objects_tier
    from examlops.data import projects

    for module in (objects_tier, projects):
        src = Path(module.__file__).read_text()
        assert '"MLFLOW_S3_ENDPOINT_URL", "http://localhost:9000"' not in src, (
            f"{module.__name__} still defaults to :9000, a port nothing in this project serves"
        )
        assert '"MLFLOW_S3_ENDPOINT_URL", "http://localhost:19000"' in src
