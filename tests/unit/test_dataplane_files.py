"""ADR 0130 — files connector: local fsspec paths, globbing, formats, incremental, http guard."""

from __future__ import annotations

import contextlib
import http.server
import sys
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors.files import (  # noqa: E402
    _PARQUET_BATCH,
    FilesConnector,
    _batches,
    _fsspec_url,
    _http_headers,
    _refuse_unsafe_fs,
    _storage_options,
)
from examlops.dataplane.types import EgressDenied, LimitExceeded, Limits, SpecError  # noqa: E402

# --- task 22a: bounded-memory parquet reads -----------------------------------------------------


def test_parquet_batches_are_bounded_and_preserve_total_row_count(tmp_path):
    """A 287 MB real-world Parquet pull (231k rows, one row group, wide list<int32> columns) OOM-
    killed the dataplane container streaming it at the old 65,536-row batch size. A generated
    single-row-group file with a list<int32> column must now come back in batches of at most
    ``_PARQUET_BATCH`` rows, with the total row count preserved.
    """
    n_rows = 20_000
    table = pa.Table.from_pylist(
        [{"id": i, "trace": [i, i + 1, i + 2]} for i in range(n_rows)],
        schema=pa.schema([("id", pa.int64()), ("trace", pa.list_(pa.int32()))]),
    )
    path = tmp_path / "wide.parquet"
    pq.write_table(table, path, row_group_size=n_rows)  # force exactly one row group
    with open(path, "rb") as fh:
        batches = list(_batches(fh, "parquet"))
    assert batches, "expected at least one batch"
    assert all(b.num_rows <= _PARQUET_BATCH for b in batches)
    assert sum(b.num_rows for b in batches) == n_rows


def _handler_factory(*, bodies, extra_headers, captured):
    """A stdlib handler that serves ``bodies[i]`` on the i-th request (clamped to the last one)."""
    state = {"n": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, *, include_body: bool) -> None:
            if captured is not None:
                captured.append(dict(self.headers.items()))
            body = bodies[min(state["n"], len(bodies) - 1)]
            state["n"] += 1
            self.send_response(200)
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(include_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(include_body=False)

        def log_message(self, *a: object) -> None:  # silence per-request stderr noise
            pass

    return _Handler


@contextlib.contextmanager
def _local_server(*, bodies=(b"ok",), headers=None, captured=None):
    """A stdlib HTTP server on an OS-assigned loopback port — never one of conftest's live ports."""
    httpd = http.server.HTTPServer(
        ("127.0.0.1", 0),
        _handler_factory(bodies=list(bodies), extra_headers=headers, captured=captured),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


@pytest.fixture
def drop(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    d = tmp_path / "drop"
    d.mkdir()
    pq.write_table(pa.Table.from_pylist([{"a": 1}, {"a": 2}]), d / "2026-01.parquet")
    (d / "extra.csv").write_text("a,b\n3,x\n4,y\n")
    (d / "events.jsonl").write_text('{"a": 5}\n{"a": 6}\n')
    return d


def _read(spec, since=None):
    return list(FilesConnector().read(None, spec, since, Limits()))


def test_parquet_glob_names_the_table_after_the_file_stem(drop):
    batches = _read({"url": f"file://{drop}", "glob": "*.parquet"})
    assert {tb.table for tb in batches} == {"2026-01"}
    assert sum(tb.batch.num_rows for tb in batches) == 2


def test_csv_and_jsonl(drop):
    assert sum(tb.batch.num_rows for tb in _read({"url": f"file://{drop}", "glob": "*.csv"})) == 2
    assert [
        r["a"]
        for tb in _read({"url": f"file://{drop}", "glob": "*.jsonl"})
        for r in tb.batch.to_pylist()
    ] == [5, 6]


def test_table_override(drop):
    assert {
        tb.table
        for tb in _read({"url": f"file://{drop}", "glob": "*.parquet", "table": "job_table"})
    } == {"job_table"}


def test_incremental_skips_unchanged_files(drop):
    spec = {"url": f"file://{drop}", "glob": "*.parquet", "incremental": True}
    wm = _read(spec)[-1].watermark
    assert _read(spec, wm) == []
    pq.write_table(pa.Table.from_pylist([{"a": 9}]), drop / "2026-02.parquet")
    assert {tb.table for tb in _read(spec, wm) if tb.batch.num_rows} == {"2026-02"}


def test_local_files_refused_by_default(drop, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES")
    with pytest.raises(SpecError, match="local files"):
        _read({"url": f"file://{drop}"})


def test_http_goes_through_the_egress_guard():
    with pytest.raises(EgressDenied):
        _read({"url": "http://127.0.0.1:9/data.csv"})


def test_schemeless_url_is_refused():
    with pytest.raises(SpecError, match="scheme"):
        _read({"url": "/tmp/does-not-matter"})


def test_chained_fsspec_protocol_is_refused(drop):
    with pytest.raises(SpecError, match="chained"):
        _read({"url": f"simplecache::file://{drop}", "glob": "*.parquet"})


def test_validate_spec_rejects_schemeless_and_chained_url():
    conn = FilesConnector()
    assert any("scheme" in e for e in conn.validate_spec({"url": "/tmp/x"}))
    assert any("chain" in e for e in conn.validate_spec({"url": "simplecache::file:///tmp/x"}))


def test_glob_with_dotdot_is_rejected_by_read(drop):
    with pytest.raises(SpecError, match=r"\.\."):
        _read({"url": f"file://{drop}", "glob": "../*.parquet"})


def test_validate_spec_rejects_dotdot_glob():
    errors = FilesConnector().validate_spec({"url": "file:///tmp/x", "glob": "../etc/*"})
    assert any(".." in e for e in errors)


def test_http_headers_helper_builds_bearer_auth_only_for_the_connections_own_origin():
    # Final review C1: the bearer goes only to the origin the connection itself configures.
    conn = {"uri": "https://data.example/exports/", "secret": "tok123"}
    assert _http_headers(conn, "https://data.example/x.csv") == {"Authorization": "Bearer tok123"}
    assert _http_headers(conn, "https://DATA.example:443/y.csv") == {
        "Authorization": "Bearer tok123"
    }
    assert _http_headers(conn, "https://evil.example/x.csv") == {}
    assert _http_headers(conn, "http://data.example/x.csv") == {}  # another scheme
    assert _http_headers(conn, "https://data.example:8443/x.csv") == {}  # another port
    assert _http_headers({"secret": "tok123"}, "https://data.example/x.csv") == {}  # no origin
    assert _http_headers(None, "https://data.example/x.csv") == {}
    assert _http_headers({}, "https://data.example/x.csv") == {}


def test_probe_http_sends_the_same_auth_header_as_read(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    captured: list[dict[str, str]] = []
    with _local_server(captured=captured) as port:
        conn = {"kind": "uri", "uri": f"http://127.0.0.1:{port}", "secret": "tok123"}
        FilesConnector().probe(conn, {"url": f"http://127.0.0.1:{port}/data.csv"})
    assert captured[-1].get("Authorization") == "Bearer tok123"


def test_http_download_enforces_max_bytes(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    big = b"x" * 200_000
    with _local_server(bodies=[big]) as port:
        limits = Limits(max_bytes=1_000)
        spec = {"url": f"http://127.0.0.1:{port}/big.csv"}
        with pytest.raises(LimitExceeded):
            list(FilesConnector().read(None, spec, None, limits))


def test_incremental_http_uses_content_hash_when_no_etag(monkeypatch):
    """With no validator header, the content hash is what tells a changed body apart. A changed
    body is not an append (the file is one table the parent snapshot already holds), so the read
    signals ``IncrementalInvalidated`` and ``run_pull`` re-reads it in full (ADR 0130, fix I1)."""
    from examlops.dataplane.types import IncrementalInvalidated

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    with _local_server(bodies=[b"a,b\n1,x\n", b"a,b\n1,x\n2,y\n"]) as port:
        spec = {"url": f"http://127.0.0.1:{port}/data.csv", "incremental": True}
        first = _read(spec)
        assert sum(tb.batch.num_rows for tb in first) == 1
        wm = first[-1].watermark
        assert wm["files"]["data.csv"].startswith("sha256:")
        with pytest.raises(IncrementalInvalidated, match="changed"):
            _read(spec, wm)


def test_incremental_http_skips_when_content_hash_is_unchanged(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    same = b"a,b\n1,x\n"
    with _local_server(bodies=[same, same]) as port:
        spec = {"url": f"http://127.0.0.1:{port}/data.csv", "incremental": True}
        first = _read(spec)
        wm = first[-1].watermark
        assert _read(spec, wm) == []


# --- fix round 2: bare colon schemes without a "//" authority ---------------------------------


@pytest.mark.parametrize("bad", ["s3:x/y.parquet", "hdfs:x", "sftp:x", "gs:x", "abfs:x"])
def test_bare_colon_scheme_is_refused_at_validate_spec(bad):
    errors = FilesConnector().validate_spec({"url": bad})
    assert any("://" in e for e in errors), errors


@pytest.mark.parametrize("bad", ["s3:x/y.parquet", "hdfs:x/y.parquet", "sftp:x/y.parquet"])
def test_bare_colon_scheme_is_refused_at_read_even_with_a_matching_local_folder(
    bad, tmp_path, monkeypatch
):
    # A cwd containing a directory literally named e.g. "s3:" is exactly what would let fsspec's
    # "no protocol matched" fallback quietly resolve to a real local file if the scheme/authority
    # check were missing — prove the connector refuses before ever touching local disk.
    scheme = bad.split(":", 1)[0]
    trap = tmp_path / f"{scheme}:"
    (trap / "x").mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"a": 1}]), trap / "x" / "y.parquet")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SpecError, match="://"):
        _read({"url": bad})


def test_refuse_unsafe_fs_blocks_a_real_local_filesystem(monkeypatch):
    from fsspec.implementations.local import LocalFileSystem

    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    with pytest.raises(SpecError, match="local filesystem"):
        _refuse_unsafe_fs(LocalFileSystem(), "s3")


def test_refuse_unsafe_fs_blocks_a_duck_typed_local_protocol(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)

    class _FakeLocalFs:
        protocol = "local"

    with pytest.raises(SpecError, match="local filesystem"):
        _refuse_unsafe_fs(_FakeLocalFs(), "gs")


def test_refuse_unsafe_fs_allows_when_local_files_are_allowed(monkeypatch):
    from fsspec.implementations.local import LocalFileSystem

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    assert _refuse_unsafe_fs(LocalFileSystem(), "file") is None  # admitted: no raise


# --- fix round 2: sftp storage options duplicated `host` ---------------------------------------


def test_sftp_storage_options_omit_host_when_the_url_has_one():
    opts = _storage_options("sftp", {"username": "u", "secret": "p"}, "sftp://h/x")
    assert "host" not in opts
    assert opts["username"] == "u"
    assert opts["password"] == "p"


def test_sftp_storage_options_fall_back_to_connection_host_when_url_has_none():
    opts = _storage_options("sftp", {"host": "cfg-host", "secret": "p"}, "sftp:///x")
    assert opts["host"] == "cfg-host"


def test_sftp_storage_options_build_a_filesystem_without_a_duplicate_host_kwarg(monkeypatch):
    """The plan-snippet bug: passing `host=` alongside fsspec's own URL-derived `host` made
    `fsspec.core.url_to_fs("sftp://h/x", **opts)` raise
    ``TypeError: ...got multiple values for keyword argument 'host'`` for every sftp source.
    paramiko IS installed in this environment, so ``SFTPFileSystem`` imports for real; only its
    ``_connect`` (which opens the actual SSH session) is monkeypatched — no network I/O happens.
    """
    from fsspec.implementations.sftp import SFTPFileSystem

    monkeypatch.setattr(SFTPFileSystem, "_connect", lambda self: None)
    import fsspec

    url = "sftp://round3-basic-host/x"
    opts = _storage_options("sftp", {"username": "u", "secret": "p"}, url)
    fs, _path = fsspec.core.url_to_fs(url, **opts)  # must not raise TypeError
    assert isinstance(fs, SFTPFileSystem)
    assert fs.host == "round3-basic-host"
    assert fs.ssh_kwargs.get("username") == "u"
    assert fs.ssh_kwargs.get("password") == "p"


def test_sftp_url_username_wins_over_the_connection_username(monkeypatch):
    """R2 residual: fsspec also derives `username`/`password` from the URL's netloc, and the
    same `_storage_options` always supplied the connection's `username` unconditionally — so
    `sftp://user@host/path` raised the same duplicate-kwarg `TypeError`, just for `username`
    instead of `host`."""
    from fsspec.implementations.sftp import SFTPFileSystem

    monkeypatch.setattr(SFTPFileSystem, "_connect", lambda self: None)
    import fsspec

    url = "sftp://round3-user@round3-user-host/x"
    opts = _storage_options("sftp", {"username": "cfg-user", "secret": "cfg-pw"}, url)
    fs, _path = fsspec.core.url_to_fs(url, **opts)  # must not raise TypeError
    assert fs.ssh_kwargs.get("username") == "round3-user"  # the URL's own username wins
    assert fs.ssh_kwargs.get("password") == "cfg-pw"  # URL had none, so the conn secret fills in


def test_sftp_url_password_is_never_overridden_by_the_connection_secret(monkeypatch):
    from fsspec.implementations.sftp import SFTPFileSystem

    monkeypatch.setattr(SFTPFileSystem, "_connect", lambda self: None)
    import fsspec

    url = "sftp://round3-u:round3-pw@round3-userpw-host/x"
    opts = _storage_options("sftp", {"username": "cfg-user", "secret": "cfg-pw"}, url)
    fs, _path = fsspec.core.url_to_fs(url, **opts)  # must not raise TypeError
    assert fs.ssh_kwargs.get("username") == "round3-u"
    assert fs.ssh_kwargs.get("password") == "round3-pw"  # never replaced by the conn secret


def test_sftp_url_with_empty_host_builds_without_a_typeerror(monkeypatch):
    """`sftp://:22/x` — `infer_storage_options` still returns a `host` key (value ``""``); that
    is a *present* key, not a missing one, so `_storage_options` must not also supply its own
    `host` (checking truthiness instead of key membership was the earlier bug)."""
    from fsspec.implementations.sftp import SFTPFileSystem

    monkeypatch.setattr(SFTPFileSystem, "_connect", lambda self: None)
    import fsspec

    url = "sftp://:22/x"
    opts = _storage_options("sftp", {"host": "cfg-fallback-host"}, url)
    fs, _path = fsspec.core.url_to_fs(url, **opts)  # must not raise TypeError
    assert fs.host == ""  # the URL's own (empty) value, not the connection's fallback


def test_sftp_url_query_string_is_stripped_before_reaching_fsspec(monkeypatch):
    """fsspec turns a `?...` suffix into a `url_query` kwarg meaningless to paramiko's
    `connect()`; the connector must strip it from the URL before fsspec ever parses it."""
    from fsspec.implementations.sftp import SFTPFileSystem

    monkeypatch.setattr(SFTPFileSystem, "_connect", lambda self: None)
    import fsspec

    url = "sftp://round3-query-host/x?a=1"
    fsspec_url = _fsspec_url("sftp", url)
    assert "?" not in fsspec_url
    opts = _storage_options("sftp", {}, url)
    fs, _path = fsspec.core.url_to_fs(fsspec_url, **opts)  # must not raise TypeError
    assert "url_query" not in fs.ssh_kwargs
    assert fs.host == "round3-query-host"


# --- fix round 3: discover()/_list() on an http(s) URL bypassed the egress guard via fsspec ----


def test_discover_http_goes_through_the_egress_guard(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    captured: list[dict[str, str]] = []
    with _local_server(captured=captured) as port:
        with pytest.raises(EgressDenied):
            FilesConnector().discover(None, {"url": f"http://127.0.0.1:{port}/x.csv"})
    assert captured == []  # the server never saw a connection


def test_discover_http_works_through_the_guarded_client_with_the_allow_list(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    captured: list[dict[str, str]] = []
    with _local_server(captured=captured) as port:
        infos = FilesConnector().discover(None, {"url": f"http://127.0.0.1:{port}/x.csv"})
    assert [i.name for i in infos] == ["x"]
    assert len(captured) == 1  # exactly one guarded HEAD reached the server


def test_list_refuses_http_scheme_outright_as_defence_in_depth():
    with pytest.raises((SpecError, EgressDenied)):
        FilesConnector()._list(None, {"url": "http://example.invalid/x.csv"})


def test_refuse_unsafe_fs_blocks_an_http_filesystem():
    class _FakeHttpFs:
        protocol = ("http", "https")

    with pytest.raises(EgressDenied):
        _refuse_unsafe_fs(_FakeHttpFs(), "https")
