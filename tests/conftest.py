"""Shared pytest fixtures."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# CLI output under test must be the plain text a script sees, wherever the suite runs. typer
# forces a colour terminal when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set — read ONCE, when
# `typer.rich_utils` is imported — so on GitHub every `--help` assertion met ANSI escapes and
# three tests failed only in CI (2026-09-10), while every local gate was green. This runs before
# anything imports typer: drop the colour-forcing variables, and use typer's own off-switch for
# the one we must not unset (GITHUB_ACTIONS, which other code may read).
for _var in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
    os.environ.pop(_var, None)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
_STARTED: pytest.StashKey[float] = pytest.StashKey()
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))

# Job directories go to a temporary place, never the checkout (BL-074). A scheduler adapter's
# working directory holds job folders, generated scripts and logs — files carrying absolute local
# paths. Their defaults are relative (`slurm_jobs`) or, for the mock, the cache directory, so a
# suite run from the repository used to leave them in it. `_no_trace_in_the_checkout` below is the
# guard that keeps it that way.
_JOB_WORKDIR = tempfile.mkdtemp(prefix="examlops-test-jobs-")
os.environ.setdefault("EXAMLOPS_HPC_WORKDIR", _JOB_WORKDIR)


#: SQLite writes these beside a database while it is open. One belonging to a file that was
#: already there is not this test's doing — on a dev host the live stack writes to the checkout's
#: own `platform.db` while the suite runs, and a guard that blamed the running test for that would
#: fail at random.
_SIDECARS = ("-wal", "-shm", "-journal")


def _test_authored(new: set[str], before: set[str]) -> set[str]:
    """The new entries a test is actually responsible for."""
    return {
        name
        for name in new
        if not any(name.endswith(suffix) and name[: -len(suffix)] in before for suffix in _SIDECARS)
    }


def assert_no_trace(before: set[str], root: Path) -> None:
    """Fail when ``root`` gained an entry this test is responsible for.

    A function rather than only fixture body, so a test can check the rule against its own
    directory: driving it by repointing the fixture's root would make *this* test the one that
    writes into the checkout.
    """
    left = sorted(_test_authored(set(os.listdir(root)) - before, before))
    assert not left, (
        f"this test left {left} in the checkout. Point it at tmp_path (or, for a scheduler, at "
        "EXAMLOPS_HPC_WORKDIR, which this suite already sets) — the repository is not scratch space."
    )


@pytest.fixture(autouse=True)
def _no_trace_in_the_checkout():
    """A unit test leaves nothing behind in the repository.

    Measured across the whole suite, four tests did: MLflow artifacts (`mlruns/`), two scheduler
    job directories (`slurm_jobs/`, `flux_jobs/`) and a `.pytest_cache` from an `exa` command that
    runs pytest. Each was invisible only because a *machine-local* ignore file hid it — a fresh
    clone has none of those, so a whole-tree `add` on another machine would publish local run data
    and generated scripts holding absolute paths. Failing here names the test that did it, in the
    change that introduces it, instead of leaving it for a future `git status`.
    """
    before = set(os.listdir(REPO_ROOT))
    yield
    assert_no_trace(before, REPO_ROOT)


# The checkout's own SQLite stores — on a dev host the live stack's. No test may open one: a unit
# test copying or initialising them is a flake (they change under it) and reads private state.
# An audit hook sees every `sqlite3.connect`, product code and libraries included (MLflow through
# SQLAlchemy too). It refuses the connection and records it, so a test fails even when the code
# under test swallows the error and degrades (2026-09-11, BL-062).
_CHECKOUT_STORES = frozenset(
    str(REPO_ROOT / n)
    for n in (
        "platform.db",
        "mlflow.db",
        "skipper_memory.db",
        "agent_memory.db",
        "skipper_review.db",
    )
)
_CHECKOUT_STORE_OPENS: list[str] = []


def _refuse_checkout_stores(event: str, args: tuple) -> None:
    if event != "sqlite3.connect" or not args:
        return
    try:
        path = os.path.realpath(os.fsdecode(args[0]))
    except (TypeError, ValueError):
        return
    if path in _CHECKOUT_STORES:
        _CHECKOUT_STORE_OPENS.append(path)
        raise PermissionError(f"a unit test opened the checkout's own store {path}")


sys.addaudithook(_refuse_checkout_stores)


@pytest.fixture(autouse=True)
def _isolate_postgres_state():
    """Give every test an empty platform database when running on Postgres.

    The mechanism lives in :mod:`examlops.storage.testing` because the dashboard's suite — a
    separate app with its own connection adapter — needs exactly the same thing, and a copy in
    two conftests would drift. No-op on SQLite, where each test gets its own ``tmp_path`` file.
    """
    from examlops.storage.testing import postgres_isolation

    yield from postgres_isolation()


@pytest.fixture(autouse=True)
def _isolate_platform_db(tmp_path, monkeypatch):
    """Give every test its own ``PLATFORM_DB`` file, before the test body runs.

    Roughly forty test modules set ``os.environ["PLATFORM_DB"] = str(tmp_path / ...)`` directly
    rather than through ``monkeypatch``, so the value outlives the test that set it and the next
    test in the same process inherits a path belonging to a test that has finished. Serially that
    is invisible — the directory still exists and the stale database is simply unused. Under
    ``-n auto`` it is not: the run reported four failures that every one of those tests passes on
    its own, all of them a `--json` command whose output would not parse because
    ``warning: platform datastore unavailable`` had been printed ahead of the JSON.

    The warning was correct and went to stderr; ``CliRunner`` merges the streams, which is why it
    landed in the parsed output. The defect is the leaked path, and it is the kind that gets
    blamed on parallelism and "fixed" by going back to a nine-minute serial run.

    ``monkeypatch.setenv`` here undoes *any* assignment made during the test, including a direct
    ``os.environ`` write, so the leak is closed for the sloppy modules without editing forty
    files — and a test that deliberately points at its own path still wins, because this runs
    first. No-op in effect on Postgres, where ``PLATFORM_DB`` is not consulted.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    yield


#: The platform's other SQLite stores whose defaults are relative to the working directory — the
#: repository root when the suite runs, which on a dev host is where the live stack keeps them.
_CWD_RELATIVE_STORES = ("AGENT_MEMORY_DB", "AGENT_DB", "AGENT_MEMORY_REVIEW_DB", "MLFLOW_SQLITE_DB")


@pytest.fixture(autouse=True)
def _no_checkout_store_opened():
    """Fail a test that connected to one of the checkout's own SQLite stores (see the hook)."""
    del _CHECKOUT_STORE_OPENS[:]
    yield
    opened = sorted(set(_CHECKOUT_STORE_OPENS))
    del _CHECKOUT_STORE_OPENS[:]
    assert not opened, (
        f"this test connected to the checkout's own store(s) {opened} — point it at tmp_path "
        "(PLATFORM_DB / MLFLOW_TRACKING_URI / AGENT_* / MLFLOW_SQLITE_DB)"
    )


@pytest.fixture(autouse=True)
def _isolate_sqlite_stores(tmp_path, monkeypatch):
    """Point every other CWD-relative SQLite store at a path under this test's ``tmp_path``.

    Found 2026-09-11 chasing a backup-test flake: with these unset, `exa backup`'s sqlite tier
    resolved `./skipper_memory.db`, `./agent_memory.db` and `./mlflow.db` in the repository root
    and **copied the developer's real agent memory and MLflow databases** into the test's backup
    — whatever happened to be there, while the live stack or another xdist worker was writing
    them, which is a flake at best and a unit test reading private state at worst. The paths
    are never created here; a test that wants a store creates it, and one that sets its own path
    still wins, because this runs first. ``CONTROL_PLANE_DB`` is left alone: unset, it falls back
    to the ``PLATFORM_DB`` above, which is already this test's own.
    """
    for var in _CWD_RELATIVE_STORES:
        monkeypatch.setenv(var, str(tmp_path / "stores" / f"{var.lower()}.db"))
    yield


@pytest.fixture(autouse=True)
def _isolate_site_profile(tmp_path, monkeypatch):
    """No developer's site profile, data root or feature overlay reaches a test (ADR 0128).

    The CLI hides and refuses commands of modules a site profile switches off. A
    ``~/.config/examlops/site.toml`` on the machine running the suite would therefore change which
    commands exist — every guard that walks the command tree would see a different tree. Pinning
    the profile path to a file that does not exist puts every test on the default (``full``).
    """
    monkeypatch.setenv("EXAMLOPS_SITE_PROFILE", str(tmp_path / "no-site-profile.toml"))
    # setenv(""), not delenv: monkeypatch only restores a variable it recorded, and `exa instance
    # init` sets EXAMLOPS_DATA_DIR for the rest of its process. Empty means unset to every reader.
    monkeypatch.setenv("EXAMLOPS_FEATURES", "")
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", "")
    yield


@pytest.fixture(autouse=True)
def _reset_cli_output_modes():
    """Return the CLI's output globals to their defaults before every test.

    ``examlops.cli.main``'s root callback sets ``json_mode``/``quiet_mode``/``verbose_mode``/
    ``yes_mode``/``output_format`` on the module and never restores them, so a single
    ``runner.invoke(app, ["-q", ...])`` leaves quiet mode on for the rest of the process. Tests
    that invoke a *sub*-app never run that callback, so nothing clears it for them.

    The failure that makes this worth a fixture is silent: a leaked ``quiet_mode`` suppresses
    exactly the output an "it must not print X" assertion is looking for, so the test passes
    without evaluating anything. Nothing in the suite is losing that way today — checked, and the
    absence-assertions downstream of the one live leak read subprocess stdout and SQL strings, not
    ``_output`` — but it is order-dependent, and the next such test would be green on arrival.
    """
    from examlops.cli import _output

    _output.json_mode = False
    _output.quiet_mode = False
    _output.verbose_mode = False
    _output.yes_mode = False
    _output.output_format = "table"
    yield


def pytest_configure(config: pytest.Config) -> None:
    """Stamp when the session started, so the summary can tell if the tree moved under it."""
    config.stash[_STARTED] = time.time()


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    """Say so when a source file changed while the suite was running.

    A pytest run reads ``conftest.py`` once at startup and each test module once at collection, so
    a file saved a few seconds into a twenty-minute run produces a result that belongs to no
    version of the tree: part of the run saw the old file, the rest saw the new one. This repo has
    a second writer in it often enough that the failure is not hypothetical — one run reported
    ``test_the_next_test_starts_from_the_defaults_anyway`` red because the autouse fixture that
    test exists to check was written into ``conftest.py`` three seconds after collection began.
    Without this line the only way to find that out is to compare mtimes against the run window by
    hand, long after the log has scrolled away.

    Reported, never enforced: the run's own exit status is untouched, because a mid-run edit does
    not make the result wrong, only unreliable.
    """
    started = config.stash.get(_STARTED, None)
    if started is None:
        return
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Never fail silently. This reporter is only ever *read* as an absence: no banner is
        # taken to mean the tree was settled. If it cannot run, that reading is wrong, and the
        # silence is indistinguishable from a clean run. `git` is not on the PATH of every
        # image that runs this suite — that is exactly how thirteen other guards went unnoticed
        # for months — so say so rather than return.
        terminalreporter.write_sep("=", "tree-change check did not run", yellow=True)
        terminalreporter.write_line(
            f"  `git ls-files` failed ({type(exc).__name__}: {exc}), so nothing here checked "
            "whether a file was written mid-run. Absence of a warning is not evidence.",
            yellow=True,
        )
        return
    moved = []
    for name in listing.split("\0"):
        if not name:
            continue
        try:
            if (REPO_ROOT / name).stat().st_mtime > started:
                moved.append(name)
        except OSError:
            continue
    if not moved:
        return
    terminalreporter.write_sep("=", "tree changed during this run", yellow=True)
    for name in sorted(moved)[:10]:
        terminalreporter.write_line(f"  {name}", yellow=True)
    if len(moved) > 10:
        terminalreporter.write_line(f"  … and {len(moved) - 10} more", yellow=True)
    terminalreporter.write_line(
        "These were written after collection started, so this result may mix two versions of the "
        "tree. Re-run on a settled tree before trusting it.",
        yellow=True,
    )
