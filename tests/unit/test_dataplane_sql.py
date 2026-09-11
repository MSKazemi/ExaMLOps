"""ADR 0130 — sql connector against a real SQLite file."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import sql as sql_module  # noqa: E402
from examlops.dataplane.connectors.sql import (  # noqa: E402
    SqlConnector,
    _egress_check_url,
    begin_read_only,
)
from examlops.dataplane.types import EgressDenied, Limits, SpecError  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    # A sqlite URL is a local file (fix KS): the connector refuses it unless local files are
    # explicitly enabled, the same gate the files connector applies.
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, node TEXT, power REAL, ts TEXT)"
        )
        for i in range(1, 8):
            c.exec_driver_sql(
                "INSERT INTO jobs VALUES (?,?,?,?)", (i, f"n{i % 2}", i * 10.0, f"2026-01-0{i}")
            )
    return {"kind": "sql", "url": url}


def _rows(batches):
    return [r for tb in batches for r in tb.batch.to_pylist()]


def test_validate_spec():
    c = SqlConnector()
    assert c.validate_spec({}) == ["spec.query or spec.table is required"]
    assert "identifier" in c.validate_spec({"table": "jobs; DROP TABLE x"})[0]
    assert c.validate_spec({"table": "jobs"}) == []


def test_reads_a_table_in_chunks(db):
    batches = list(SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits()))
    assert [tb.batch.num_rows for tb in batches] == [3, 3, 1]
    assert {tb.table for tb in batches} == {"jobs"}


def test_incremental_uses_the_watermark(db):
    spec = {"table": "jobs", "watermark_column": "id", "incremental": True}
    first = list(SqlConnector().read(db, spec, None, Limits()))
    wm = first[-1].watermark
    assert wm == {"column": "id", "value": 7, "type": "int"}
    with sa.create_engine(db["url"]).begin() as c:
        c.exec_driver_sql("INSERT INTO jobs VALUES (8,'n0',80.0,'2026-01-08')")
    assert [r["id"] for r in _rows(SqlConnector().read(db, spec, wm, Limits()))] == [8]


def test_query_with_params(db):
    rows = _rows(
        SqlConnector().read(
            db,
            {
                "query": "SELECT id FROM jobs WHERE node = :n",
                "params": {"n": "n1"},
                "output": "odd",
            },
            None,
            Limits(),
        )
    )
    assert [r["id"] for r in rows] == [1, 3, 5, 7]


def test_session_is_read_only(db):
    eng = sa.create_engine(db["url"])
    with eng.connect() as c:
        begin_read_only(c, timeout_s=5)
        with pytest.raises(sa.exc.OperationalError):
            c.exec_driver_sql("INSERT INTO jobs VALUES (99,'x',1.0,'x')")


def test_password_never_appears_in_errors(tmp_path):
    conn = {
        "kind": "sql",
        "url": "postgresql+psycopg://bob@127.0.0.1:1/nope",
        "secret": "hunter2hunter2",
    }
    probe = SqlConnector().probe(conn)
    assert probe.ok is False and "hunter2" not in probe.detail


@pytest.fixture
def disposals(monkeypatch):
    """Wrap ``sa.create_engine`` so every disposal of an engine it built is recorded."""
    disposed = []
    real_create_engine = sa.create_engine

    def wrapper(*a, **kw):
        eng = real_create_engine(*a, **kw)
        orig_dispose = eng.dispose

        def dispose(*da, **dkw):
            disposed.append(eng)
            return orig_dispose(*da, **dkw)

        eng.dispose = dispose
        return eng

    monkeypatch.setattr(sa, "create_engine", wrapper)
    return disposed


def test_engine_is_disposed_after_a_full_read(db, disposals):
    list(SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits()))
    assert len(disposals) == 1


def test_engine_is_disposed_when_a_read_generator_is_closed_early(db, disposals):
    gen = SqlConnector().read(db, {"table": "jobs", "chunk_rows": 3}, None, Limits())
    next(gen)
    gen.close()
    assert len(disposals) == 1


def test_engine_is_disposed_after_probe(db, disposals):
    assert SqlConnector().probe(db).ok is True
    assert len(disposals) == 1


def test_engine_is_disposed_after_discover(db, disposals):
    SqlConnector().discover(db, {})
    assert len(disposals) == 1


# --- fix KS: sql connections egress-checked (ADR 0130 §10) --------------------------------------


def test_egress_check_refuses_a_platform_internal_bootstrap_host(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    url = make_url("postgresql+psycopg://bob:pw@postgres/db")
    with pytest.raises(EgressDenied, match="platform-internal"):
        _egress_check_url(url)


def test_egress_check_refuses_a_loopback_host(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    url = make_url("mysql+pymysql://bob:pw@127.0.0.1:3307/db")
    with pytest.raises(EgressDenied):
        _egress_check_url(url)


def test_egress_check_admits_an_allow_listed_host_and_defaults_the_postgres_port(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    real = sql_module.check_address

    def spy(host, port, **kw):
        calls.append((host, port))
        return real(host, port, **kw)

    monkeypatch.setattr(sql_module, "check_address", spy)
    url = make_url("postgresql+psycopg://bob:pw@127.0.0.1/db")  # no port -> default 5432
    _egress_check_url(url)
    assert calls == [("127.0.0.1", 5432)]


def test_egress_check_defaults_the_mysql_port(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    real = sql_module.check_address

    def spy(host, port, **kw):
        calls.append((host, port))
        return real(host, port, **kw)

    monkeypatch.setattr(sql_module, "check_address", spy)
    url = make_url("mysql+pymysql://bob:pw@127.0.0.1/db")  # no port -> default 3306
    _egress_check_url(url)
    assert calls == [("127.0.0.1", 3306)]


def test_egress_check_pins_hostaddr_for_postgresql_psycopg(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    url = make_url("postgresql+psycopg://bob:pw@127.0.0.1:5999/db")
    assert _egress_check_url(url) == {"hostaddr": "127.0.0.1"}


def test_egress_check_does_not_pin_other_dialects(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    assert _egress_check_url(make_url("mysql+pymysql://bob:pw@127.0.0.1:3307/db")) == {}
    assert _egress_check_url(make_url("postgresql+psycopg2://bob:pw@127.0.0.1:5999/db")) == {}


def test_egress_check_refuses_a_sqlite_url_by_default():
    url = make_url("sqlite:////tmp/does-not-matter.db")
    with pytest.raises(SpecError, match="local"):
        _egress_check_url(url)


def test_egress_check_admits_a_sqlite_url_when_local_files_are_enabled(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    url = make_url("sqlite:////tmp/does-not-matter.db")
    assert _egress_check_url(url) == {}


def test_probe_is_refused_for_a_platform_internal_postgres_host(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    conn = {"kind": "sql", "url": "postgresql+psycopg://bob:pw@postgres/db"}
    probe = SqlConnector().probe(conn)
    assert probe.ok is False
    assert "platform-internal" in probe.detail


def test_read_is_refused_for_a_sqlite_url_without_the_local_files_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    with pytest.raises(SpecError, match="local"):
        list(SqlConnector().read({"kind": "sql", "url": url}, {"table": "jobs"}, None, Limits()))


# --- fix KS round 1: CRITICAL 1 — a query-string host overrides the checked host ----------------


def test_query_host_param_is_refused_for_postgres():
    url = make_url("postgresql+psycopg://u:p@allowed.example.com/db?host=postgres")
    with pytest.raises(SpecError, match="host"):
        _egress_check_url(url)


def test_query_host_param_is_refused_for_mysql():
    url = make_url("mysql+pymysql://u:p@allowed.example.com/db?host=postgres")
    with pytest.raises(SpecError, match="host"):
        _egress_check_url(url)


def test_query_hostaddr_param_is_refused():
    """``hostaddr`` is the exact connect_args key this module's own pin uses — letting a query
    set it directly would hand an attacker the pin itself."""
    url = make_url("postgresql+psycopg://u:p@allowed.example.com/db?hostaddr=1.2.3.4")
    with pytest.raises(SpecError, match="hostaddr"):
        _egress_check_url(url)


def test_query_service_param_is_refused():
    url = make_url("postgresql+psycopg://u:p@allowed.example.com/db?service=myservice")
    with pytest.raises(SpecError, match="service"):
        _egress_check_url(url)


def test_query_port_param_is_refused():
    url = make_url("postgresql+psycopg://u:p@allowed.example.com/db?port=1234")
    with pytest.raises(SpecError, match="port"):
        _egress_check_url(url)


def test_query_unix_socket_param_is_refused_for_mysql():
    url = make_url(
        "mysql+pymysql://u:p@allowed.example.com/db?unix_socket=/var/run/mysqld/mysqld.sock"
    )
    with pytest.raises(SpecError, match="unix_socket"):
        _egress_check_url(url)


def test_query_override_is_refused_even_when_the_host_would_otherwise_be_allow_listed(monkeypatch):
    """The whole point of CRITICAL 1: an allow-listed netloc host must not launder a query-level
    override past the guard — the query key is refused outright, whatever the netloc says."""
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "allowed.example.com")
    url = make_url("postgresql+psycopg://u:p@allowed.example.com/db?host=postgres")
    with pytest.raises(SpecError, match="host"):
        _egress_check_url(url)


# --- fix KS round 1: CRITICAL 2 — a host-less/path-effective URL skips every gate ----------------


def test_multi_host_url_is_refused(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    url = make_url("postgresql+psycopg://u:p@host1,host2:6543/db")
    with pytest.raises(SpecError, match="multi-host"):
        _egress_check_url(url)


def test_a_filesystem_path_effective_host_is_refused_unless_local_files_allowed(monkeypatch):
    """Defense in depth for CRITICAL 2: whatever the mechanism, if the dialect's own resolved
    connect target is a filesystem path (a Unix socket), it is a local connection."""
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    monkeypatch.setattr(
        sql_module, "_dialect_connect_kwargs", lambda url: {"host": "/var/run/postgresql"}
    )
    url = make_url("postgresql+psycopg://u:p@ignored/db")
    with pytest.raises(SpecError, match="local"):
        _egress_check_url(url)


def test_a_filesystem_path_effective_host_is_admitted_when_local_files_allowed(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    monkeypatch.setattr(
        sql_module, "_dialect_connect_kwargs", lambda url: {"host": "/var/run/postgresql"}
    )
    url = make_url("postgresql+psycopg://u:p@ignored/db")
    assert _egress_check_url(url) == {}


def test_a_host_less_url_with_no_query_at_all_is_refused_unless_local_files_allowed(monkeypatch):
    """The gap CRITICAL 2 named directly: a host-less URL with no query override present at all
    (e.g. a driver's own default Unix-socket behaviour) used to return ``{}`` ungated."""
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    url = make_url("postgresql+psycopg://u:p@/db")
    assert url.host is None
    with pytest.raises(SpecError, match="local"):
        _egress_check_url(url)


# --- fix KS round 1: Important 5 — discover()/read() redact egress/spec failures ----------------


def test_discover_redacts_a_secret_from_an_egress_denial():
    conn = {
        "kind": "sql",
        "url": "postgresql+psycopg://bob@127.0.0.1:1/nope",
        "secret": "hunter2hunter2",
    }
    with pytest.raises(EgressDenied) as exc_info:
        SqlConnector().discover(conn, {})
    assert "hunter2" not in str(exc_info.value)


def test_read_redacts_a_secret_from_a_local_file_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    url = f"sqlite:///{tmp_path / 'lab.db'}"
    conn = {"kind": "sql", "url": url, "secret": "hunter2hunter2"}
    with pytest.raises(SpecError) as exc_info:
        list(SqlConnector().read(conn, {"table": "jobs"}, None, Limits()))
    assert "hunter2" not in str(exc_info.value)


def test_read_routes_an_egress_denial_through_the_redact_helper(monkeypatch):
    """Proves the wiring, not just the absence of a leak: ``redact()`` is actually invoked on the
    discover()/read() path, the same one ``probe()`` uses — not only when there happens to be a
    secret to strip."""
    calls: list[str] = []
    real_redact = sql_module.redact

    def spy(text, **kw):
        calls.append(text)
        return real_redact(text, **kw)

    monkeypatch.setattr(sql_module, "redact", spy)
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    conn = {"kind": "sql", "url": "postgresql+psycopg://bob@127.0.0.1:1/nope"}
    with pytest.raises(EgressDenied):
        list(SqlConnector().read(conn, {"table": "jobs"}, None, Limits()))
    assert calls


# --- fix KS round 1: Important 7 — connect_args actually reaches sa.create_engine ---------------


def test_engine_receives_the_hostaddr_pin_via_connect_args(monkeypatch):
    # Builds a real psycopg engine; the driver ships in the `dataplane-sql` extra, not `dev`.
    pytest.importorskip("psycopg")
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    captured: dict = {}
    real_create_engine = sa.create_engine

    def fake_create_engine(url, **kw):
        captured.update(kw)
        return real_create_engine(url, **kw)

    monkeypatch.setattr(sa, "create_engine", fake_create_engine)
    conn = {"kind": "sql", "url": "postgresql+psycopg://bob:pw@127.0.0.1:5999/db"}
    eng = SqlConnector()._engine(conn)
    eng.dispose()
    assert captured.get("connect_args") == {"hostaddr": "127.0.0.1"}


def test_egress_check_names_an_unknown_dialect_as_a_spec_error():
    url = make_url("nosuchdialect+nodriver://bob:pw@db.example.org/db")
    with pytest.raises(SpecError, match="nosuchdialect"):
        _egress_check_url(url)
