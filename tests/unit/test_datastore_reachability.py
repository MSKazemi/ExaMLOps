"""An unreachable datastore must cost a moment, not half a minute (note item T8).

Found while proving the Postgres backup round trip: with ``EXAMLOPS_DB_BACKEND=postgres`` and the
server down, *every* ``exa`` command took ~31 s before it ran — including commands that never touch
the datastore — because ``main.py``'s root callback opens it on every invocation and
``psycopg_pool.getconn()`` waits out its full 30 s budget while the background workers retry a
connection that is being refused instantly. An operator diagnosing an outage is exactly the person
who runs the most commands, so they paid it the most.

The fix is a bounded TCP pre-check before the pool is built, plus a visible warning instead of a
silent ``except Exception: pass``. These tests pin both, and the cases the probe deliberately does
*not* judge — unix sockets and multi-host failover DSNs, where libpq knows better than we do.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

from examlops.storage import pg  # noqa: E402


@pytest.fixture(autouse=True)
def _forget_unreachable() -> None:
    """The negative cache is process-global; a leaked entry would fake a later test's result."""
    pg._UNREACHABLE.clear()


# ── the budget ────────────────────────────────────────────────────────────────────────────────
class TestConnectTimeout:
    def test_defaults_to_two_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXAMLOPS_POSTGRES_CONNECT_TIMEOUT", raising=False)
        assert pg._connect_timeout() == 2.0

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXAMLOPS_POSTGRES_CONNECT_TIMEOUT", "7.5")
        assert pg._connect_timeout() == 7.5

    def test_garbage_falls_back_rather_than_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A typo in an env var must not be what stops the platform from opening its datastore.
        monkeypatch.setenv("EXAMLOPS_POSTGRES_CONNECT_TIMEOUT", "soon")
        assert pg._connect_timeout() == 2.0

    def test_cannot_be_configured_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 0 would make every probe fail on a healthy server; the floor keeps it a timeout.
        monkeypatch.setenv("EXAMLOPS_POSTGRES_CONNECT_TIMEOUT", "0")
        assert pg._connect_timeout() == pytest.approx(0.1)


# ── the probe ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def psycopg_mod():  # noqa: ANN201 - the module object, whatever version is installed
    """psycopg is not a declared dependency, so these skip where it is absent (e.g. plain CI)."""
    return pytest.importorskip("psycopg")


class TestRequireReachable:
    def test_refused_port_raises_quickly_and_names_the_address(self, psycopg_mod) -> None:  # noqa: ANN001
        # Port 1 is privileged and never listening: the kernel refuses instantly, so this measures
        # the code path, not the network.
        started = time.monotonic()
        with pytest.raises(psycopg_mod.OperationalError) as excinfo:
            pg._require_reachable("postgresql://u:p@127.0.0.1:1/examlops")
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, f"probe took {elapsed:.2f}s — it must fail fast, not wait"
        msg = str(excinfo.value)
        assert "127.0.0.1:1" in msg, "an operator needs the address that was tried"
        assert "EXAMLOPS_POSTGRES_DSN" in msg, "and the variable that sets it"

    def test_listening_socket_passes(self, psycopg_mod) -> None:  # noqa: ANN001, ARG002
        # A real listener on an ephemeral port — the healthy case must stay silent.
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            pg._require_reachable(f"postgresql://u:p@127.0.0.1:{port}/examlops")

    @pytest.mark.parametrize(
        ("dsn", "why"),
        [
            ("postgresql://u:p@/examlops?host=/var/run/postgresql", "unix socket has no TCP port"),
            (
                "postgresql://u:p@a.example,b.example:5432/examlops",
                "multi-host failover is libpq's",
            ),
            ("host=/tmp dbname=examlops", "key-value unix socket"),
        ],
    )
    def test_cases_libpq_handles_better_are_skipped(self, psycopg_mod, dsn: str, why: str) -> None:  # noqa: ANN001, ARG002
        # None of these hosts is reachable; the probe must decline to judge rather than block a
        # perfectly valid configuration it does not understand.
        pg._require_reachable(dsn)

    def test_unparseable_dsn_is_left_for_psycopg_to_reject(self, psycopg_mod) -> None:  # noqa: ANN001, ARG002
        # Raising our own error here would replace psycopg's precise message with a vaguer one.
        pg._require_reachable("this is not a dsn at all")

    def test_pooled_connect_probes_before_building_the_pool(
        self, psycopg_mod, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        """The probe must run *before* the pool exists, or it saves nothing."""
        monkeypatch.setattr(pg, "_POOLS", {})
        monkeypatch.setattr(pg, "_POOL_UNAVAILABLE", False)
        built = []
        monkeypatch.setattr(pg, "_ensure_schema", lambda *a, **k: built.append("schema"))

        with pytest.raises(psycopg_mod.OperationalError):
            pg.connect("postgresql://u:p@127.0.0.1:1/examlops")
        assert built == [], "the pre-check must short-circuit before any connection work"
        assert pg._POOLS == {}, "a pool to an unreachable server must not be cached"


# ── the warning ───────────────────────────────────────────────────────────────────────────────
class TestRootCallbackSurfacesTheFailure:
    """A silent ``except Exception: pass`` left the operator with no clue. It must say so."""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]):  # noqa: ANN205
        from typer.testing import CliRunner

        from examlops.cli import main as cli_main

        def _boom() -> None:
            raise RuntimeError("unreachable at 127.0.0.1:1 after 2s")

        monkeypatch.setattr(cli_main, "_init_platform_db", _boom)
        return CliRunner().invoke(cli_main.app, argv)

    def test_warns_and_still_runs_the_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._run(monkeypatch, ["plugins"])
        assert result.exit_code == 0, "an unavailable datastore is a warning, not a failure"
        assert "datastore unavailable" in result.output
        assert "unreachable at 127.0.0.1:1" in result.output, "the cause must survive"

    def test_quiet_suppresses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._run(monkeypatch, ["-q", "plugins"])
        assert result.exit_code == 0
        assert "datastore unavailable" not in result.output


# ── the negative cache ────────────────────────────────────────────────────────────────────────
class TestUnreachableIsRememberedBriefly:
    """Probing once per *connection* would multiply the budget by how often a command reconnects.

    Against a refused port that is invisible (each probe is instant); against a black-holed host
    it is a real wait, and `exa audit` opens the datastore twice. Measured before this cache:
    5.6 s with a 2 s budget. After: 3.4 s, i.e. one probe.
    """

    def test_ttl_default_override_and_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", raising=False)
        assert pg._unreachable_ttl() == 5.0
        monkeypatch.setenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", "12")
        assert pg._unreachable_ttl() == 12.0
        monkeypatch.setenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", "later")
        assert pg._unreachable_ttl() == 5.0

    def test_second_probe_within_ttl_does_not_touch_the_network(
        self, psycopg_mod, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        calls = []
        real = socket.create_connection

        def _counting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", _counting)
        dsn = "postgresql://u:p@127.0.0.1:1/examlops"

        for _ in range(3):
            with pytest.raises(psycopg_mod.OperationalError):
                pg._require_reachable(dsn)
        assert len(calls) == 1, f"probed {len(calls)}× — the cache is not holding"

    def test_the_cached_error_still_says_what_went_wrong(self, psycopg_mod) -> None:  # noqa: ANN001
        dsn = "postgresql://u:p@127.0.0.1:1/examlops"
        with pytest.raises(psycopg_mod.OperationalError):
            pg._require_reachable(dsn)
        with pytest.raises(psycopg_mod.OperationalError) as second:
            pg._require_reachable(dsn)
        assert "127.0.0.1:1" in str(second.value), "a cached failure must not become vaguer"

    def test_expiry_lets_a_recovered_server_back_in(
        self, psycopg_mod, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001, ARG002
        """A long-lived process must not be locked out once the datastore returns."""
        monkeypatch.setenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", "0")  # expire immediately
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            dsn = f"postgresql://u:p@127.0.0.1:{port}/examlops"
            pg._UNREACHABLE[("127.0.0.1", port)] = (time.monotonic() - 1, "stale: was down")
            pg._require_reachable(dsn)  # must re-probe and succeed, not replay the stale entry
        assert ("127.0.0.1", port) not in pg._UNREACHABLE, "an expired entry must be dropped"
