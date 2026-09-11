"""ADR 0130 task 22b — S3 through pyarrow's native filesystem, wrapped as an fsspec filesystem.

s3fs cannot be locked together with the workspace: every aiobotocore release caps botocore below
the root's ``boto3>=1.43.88`` floor. pyarrow is already a hard dependency of every dataplane extra
and ships ``pyarrow.fs.S3FileSystem``; ``fsspec.implementations.arrow.ArrowFSWrapper`` makes it a
normal fsspec filesystem. Nothing here reaches a real S3 service: the S3 filesystems are only
constructed, and the store/connector cycles run over local pyarrow filesystems behind the same
wrapper. The one socket opened is a loopback HTTP server that the SSRF tests prove is never hit.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import socket
import sys
import threading
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fsspec.implementations.arrow import ArrowFSWrapper  # noqa: E402

from examlops.dataplane import s3 as s3mod  # noqa: E402
from examlops.dataplane import safety  # noqa: E402
from examlops.dataplane import store as st  # noqa: E402
from examlops.dataplane.connectors.files import (  # noqa: E402
    FilesConnector,
    _guard_s3_endpoint,
    _refuse_unsafe_fs,
    _storage_options,
)
from examlops.dataplane.types import EgressDenied, Limits, SpecError, TableBatch  # noqa: E402
from pipelines.datasets.versioning import resolve_revision  # noqa: E402

# Obviously-fake credentials, assembled at runtime so no secret scanner mistakes them for real.
_KEY = "unit" + "-access"
_SECRET = "unit" + "-secret"


@pytest.fixture(autouse=True)
def _no_ambient_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)


def _opts(fs) -> dict:
    """The options a real ``pyarrow.fs.S3FileSystem`` was built with (its pickle state)."""
    assert isinstance(fs, ArrowFSWrapper)
    assert isinstance(fs.fs, pafs.S3FileSystem)
    return dict(fs.fs.__reduce__()[1][0])


def _fake_dns(monkeypatch, ip: str) -> list[str]:
    """Point ``safety.check_address``'s default resolver at ``ip``; returns the names looked up."""
    looked_up: list[str] = []
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def resolver(host, port, **_kw):
        looked_up.append(host)
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setitem(safety.check_address.__kwdefaults__, "resolver", resolver)
    return looked_up


def _s3(**kw):
    kw.setdefault("key", _KEY)
    kw.setdefault("secret", _SECRET)
    kw.setdefault("endpoint_url", "http://minio:9000")
    kw.setdefault("anonymous", False)
    return s3mod.s3_filesystem(**kw)


# --- s3_filesystem: endpoint parsing, credentials, region ------------------------------------


def test_http_endpoint_becomes_an_override_with_the_http_scheme():
    fs = _s3()
    opts = _opts(fs)
    assert opts["endpoint_override"] == "minio:9000"
    assert opts["scheme"] == "http"
    assert opts["access_key"] == _KEY and opts["secret_key"] == _SECRET
    assert opts["anonymous"] is False
    assert fs.protocol == "s3"


def test_https_endpoint_keeps_the_https_scheme_and_a_bare_host():
    opts = _opts(_s3(endpoint_url="https://s3.example.org"))
    assert opts["endpoint_override"] == "s3.example.org"
    assert opts["scheme"] == "https"


def test_an_ipv6_endpoint_keeps_its_brackets():
    assert _opts(_s3(endpoint_url="http://[fd00::5]:9000"))["endpoint_override"] == "[fd00::5]:9000"


def test_surrounding_whitespace_on_the_endpoint_is_ignored():
    assert _opts(_s3(endpoint_url="  http://minio:9000 \n"))["endpoint_override"] == "minio:9000"


@pytest.mark.parametrize("bad", ["http://minio:abc", "http://minio:99999", "http://minio:0"])
def test_an_endpoint_with_a_bad_port_is_refused_clearly(bad):
    with pytest.raises(ValueError, match="port"):
        _s3(endpoint_url=bad)


def test_an_endpoint_override_defaults_the_region_to_us_east_1():
    assert _opts(_s3())["region"] == "us-east-1"


def test_aws_region_env_is_used_when_no_region_is_given(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    opts = _opts(_s3(endpoint_url=None))
    assert opts["region"] == "eu-central-1"
    assert opts["endpoint_override"] == ""


def test_aws_default_region_is_the_second_choice(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-south-1")
    assert _opts(_s3(endpoint_url=None))["region"] == "eu-south-1"


def test_an_explicit_region_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    opts = _opts(_s3(key=None, secret=None, endpoint_url=None, region="ap-south-1"))
    assert opts["region"] == "ap-south-1"


def test_anonymous_without_credentials_never_walks_the_credential_chain():
    """A source must not borrow the platform's identity (env vars, ``~/.aws``, EC2 metadata)."""
    opts = _opts(_s3(key=None, secret=None, anonymous=True))
    assert opts["anonymous"] is True
    assert not opts["access_key"] and not opts["secret_key"]


def test_non_anonymous_without_credentials_uses_the_default_chain():
    """The operator-trusted store may run on an instance role / IRSA: no keys, not anonymous."""
    opts = _opts(_s3(key=None, secret=None, anonymous=False))
    assert opts["anonymous"] is False
    assert not opts["access_key"] and not opts["secret_key"]


def test_empty_string_credentials_count_as_unset():
    assert _opts(_s3(key="", secret="", anonymous=True))["anonymous"] is True


def test_anonymous_with_explicit_credentials_is_contradictory():
    with pytest.raises(ValueError, match="anonymous") as exc:
        _s3(anonymous=True)
    assert _SECRET not in str(exc.value)


def test_half_a_credential_pair_is_refused_without_echoing_it():
    with pytest.raises(ValueError, match="both") as exc:
        _s3(key=None)
    assert _SECRET not in str(exc.value)


@pytest.mark.parametrize(
    "bad", ["minio:9000", "minio:abc", "ftp://minio:9000", "http://", "http:///x"]
)
def test_an_endpoint_without_an_http_scheme_and_host_is_refused(bad):
    with pytest.raises(ValueError, match="endpoint"):
        _s3(endpoint_url=bad)


def test_an_endpoint_carrying_credentials_is_refused_without_echoing_them():
    with pytest.raises(ValueError, match="credentials") as exc:
        _s3(key=None, secret=None, endpoint_url=f"http://{_KEY}:{_SECRET}@minio:9000")
    assert _SECRET not in str(exc.value) and _KEY not in str(exc.value)


def test_an_endpoint_with_a_path_is_refused():
    with pytest.raises(ValueError, match="path"):
        _s3(endpoint_url="http://minio:9000/sub")


def test_filesystems_are_not_pinned_in_fsspecs_instance_cache():
    """fsspec's instance cache is a plain dict keyed on the wrapped object's repr, which is unique
    per pyarrow filesystem — so without ``skip_instance_cache`` every call would leak one S3
    client (and its connection pool) for the life of the process."""
    before = len(ArrowFSWrapper._cache)
    a, b = _s3(), _s3()
    assert a is not b
    assert len(ArrowFSWrapper._cache) == before


def test_a_pyarrow_without_s3_support_gives_a_clear_error(monkeypatch):
    monkeypatch.delattr(pafs, "S3FileSystem")
    monkeypatch.setattr(pafs, "_not_imported", [*pafs._not_imported, "S3FileSystem"])
    with pytest.raises(ImportError, match="S3 support"):
        _s3()


def test_the_s3_filesystem_passes_the_files_connectors_unsafe_fs_check():
    fs = _s3()
    _refuse_unsafe_fs(fs, "s3")  # raises on an http(s) or local filesystem
    assert fs.protocol == "s3"


# --- url_to_fs: s3fs-style option translation ------------------------------------------------


def test_url_to_fs_translates_client_kwargs_and_strips_the_scheme():
    fs, path = s3mod.url_to_fs(
        "s3://bucket/prefix/",
        key=_KEY,
        secret=_SECRET,
        client_kwargs={"endpoint_url": "http://minio:9000"},
    )
    assert path == "bucket/prefix"
    opts = _opts(fs)
    assert opts["endpoint_override"] == "minio:9000" and opts["scheme"] == "http"
    assert opts["access_key"] == _KEY and opts["secret_key"] == _SECRET


def test_url_to_fs_accepts_a_top_level_endpoint_url_and_a_region():
    fs, path = s3mod.url_to_fs(
        "s3://bucket", endpoint_url="https://s3.example.org:9443", region="eu-west-3"
    )
    assert path == "bucket"
    opts = _opts(fs)
    assert opts["endpoint_override"] == "s3.example.org:9443" and opts["scheme"] == "https"
    assert opts["region"] == "eu-west-3" and opts["anonymous"] is True


def test_url_to_fs_anon_defaults_to_no_credentials_and_can_be_turned_off():
    assert _opts(s3mod.url_to_fs("s3://bucket/k")[0])["anonymous"] is True
    assert _opts(s3mod.url_to_fs("s3://bucket/k", anon=False)[0])["anonymous"] is False
    with_keys, _ = s3mod.url_to_fs("s3://bucket/k", key=_KEY, secret=_SECRET)
    assert _opts(with_keys)["anonymous"] is False


def test_url_to_fs_accepts_client_kwargs_region_name():
    fs, _ = s3mod.url_to_fs("s3://bucket/k", client_kwargs={"region_name": "eu-north-1"})
    assert _opts(fs)["region"] == "eu-north-1"


def test_url_to_fs_keeps_a_glob_in_the_path():
    _fs, path = s3mod.url_to_fs("s3://data-lake/pm100/*.parquet")
    assert path == "data-lake/pm100/*.parquet"


def test_url_to_fs_refuses_an_option_it_cannot_honour():
    """Silently dropping e.g. ``token`` or ``use_ssl`` would change who/how the request runs."""
    with pytest.raises(ValueError, match="token"):
        s3mod.url_to_fs("s3://bucket/k", token="t")
    with pytest.raises(ValueError, match="verify"):
        s3mod.url_to_fs("s3://bucket/k", client_kwargs={"verify": False})


def test_url_to_fs_refuses_an_s3_url_without_a_bucket():
    with pytest.raises(ValueError, match="bucket"):
        s3mod.url_to_fs("s3://")
    with pytest.raises(ValueError, match="bucket"):
        s3mod.url_to_fs("s3:///key")


def test_url_to_fs_refuses_a_bare_colon_s3_url():
    """``store_from_env`` only special-cases the ``s3://`` prefix, so ``s3:x`` reaches here."""
    with pytest.raises(ValueError, match="s3://"):
        s3mod.url_to_fs("s3:bucket/key")


def test_url_to_fs_accepts_the_files_connectors_own_s3_options():
    opts = _storage_options(
        "s3",
        {"access_key": _KEY, "secret": _SECRET, "region": "eu-west-1"},
        "s3://b/k",
        s3_endpoint="https://s3.example.org",
    )
    fs, path = s3mod.url_to_fs("s3://b/k", **opts)
    o = _opts(fs)
    assert path == "b/k" and o["endpoint_override"] == "s3.example.org"
    assert o["region"] == "eu-west-1" and o["anonymous"] is False


def test_s3_never_reaches_fsspecs_own_resolver(monkeypatch):
    import fsspec.core

    def boom(*a, **k):
        raise AssertionError("s3:// must not go through fsspec.core.url_to_fs (s3fs)")

    monkeypatch.setattr(fsspec.core, "url_to_fs", boom)
    fs, _ = s3mod.url_to_fs("s3://bucket/k", key=_KEY, secret=_SECRET)
    assert fs.protocol == "s3"


def test_other_schemes_are_delegated_to_fsspec_unchanged(monkeypatch, tmp_path):
    import fsspec.core

    seen = {}
    real = fsspec.core.url_to_fs

    def spy(url, **kw):
        seen["call"] = (url, kw)
        return real(url, **kw)

    monkeypatch.setattr(fsspec.core, "url_to_fs", spy)
    from fsspec.implementations.local import LocalFileSystem

    fs, path = s3mod.url_to_fs(f"file://{tmp_path}", auto_mkdir=True)
    assert isinstance(fs, LocalFileSystem)
    assert path == str(tmp_path)
    assert seen["call"] == (f"file://{tmp_path}", {"auto_mkdir": True})


# --- the dataset store over the wrapper ------------------------------------------------------


def _pull(store, tmp_path, values, *, key, pull_id, parent=None, incremental=False):
    w = st.SnapshotWriter(tmp_path / f"stage-{pull_id}", limits=Limits())
    for chunk in values:
        w.write(
            TableBatch(
                "jobs",
                pa.RecordBatch.from_pylist([{"id": v, "power": v * 1.5} for v in chunk]),
                {"column": "id", "value": max(chunk), "type": "int"},
            )
        )
    return st.publish(
        store,
        key,
        staged=w.close(),
        parent=parent,
        connector="files",
        connection="lake",
        spec_hash="h",
        watermark=w.watermark,
        pull_id=pull_id,
        incremental=incremental,
    )


def test_full_store_cycle_over_an_arrow_fs_wrapper(tmp_path):
    """publish -> resolve -> read_manifest -> materialize -> prune, every store operation going
    through ``ArrowFSWrapper`` exactly as it does for S3 (a local pyarrow filesystem stands in for
    the bucket)."""
    root = tmp_path / "bucket" / "dataplane"
    store = st.DatasetStore(
        ArrowFSWrapper(pafs.LocalFileSystem(), skip_instance_cache=True),
        str(root),
        uri_prefix="s3://bucket/dataplane",
    )
    key = "_global/pm100"

    m1, changed = _pull(store, tmp_path, [[1, 2], [3]], key=key, pull_id="p1")
    assert changed and m1.row_count == 3
    assert json.loads((root / key / "_latest").read_text()) == {
        "revision": m1.revision,
        "pull_id": "p1",
    }

    again, changed = _pull(store, tmp_path, [[1, 2], [3]], key=key, pull_id="p1b", parent=m1)
    assert changed is False and again.revision == m1.revision

    m2, _ = _pull(store, tmp_path, [[4]], key=key, pull_id="p2", parent=m1, incremental=True)
    assert m2.row_count == 4 and m2.parent_revision == m1.revision
    m3, _ = _pull(store, tmp_path, [[5]], key=key, pull_id="p3", parent=m2)

    ref = st.resolve(store, key)
    assert (ref.revision, ref.pull_id) == (m3.revision, "p3")
    assert st.resolve(store, key, m1.revision).pull_id == "p1"
    assert st.read_manifest(store, ref).row_count == 1
    assert store.uri(ref.manifest_key) == f"s3://bucket/dataplane/{key}/p3/_manifest.json"

    ref2 = st.resolve(store, key, m2.revision)
    local = st.materialize(store, ref2, tmp_path / "cache")
    assert resolve_revision("dataplane", "pm100", data_path=local).revision_id == m2.revision
    assert sorted(p.name for p in (local / "jobs").iterdir()) == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]

    assert store.ls_dirs(key) == ["_revisions", "p1", "p2", "p3"]
    removed = st.prune(store, key, keep=1, pinned={m2.revision})
    # p2 is pinned and still carries p1's part (incremental) — only nothing-referenced dirs go.
    assert removed == []
    removed = st.prune(store, key, keep=1, pinned=set())
    assert set(removed) == {"p1", "p2"}
    assert store.ls_dirs(key) == ["_revisions", "p3"]
    assert not store.exists(f"{key}/_revisions/{m1.revision}")
    assert st.resolve(store, key).pull_id == "p3"


def test_store_from_url_builds_s3_through_pyarrow():
    store = st.DatasetStore.from_url(
        "s3://bucket/dataplane",
        key=_KEY,
        secret=_SECRET,
        client_kwargs={"endpoint_url": "http://minio:9000"},
    )
    assert store.root == "bucket/dataplane"
    assert store.uri("x") == "s3://bucket/dataplane/x"
    assert _opts(store.fs)["endpoint_override"] == "minio:9000"


@pytest.fixture
def store_env(monkeypatch):
    for name in (
        "EXAMLOPS_DATAPLANE_STORE_URL",
        "EXAMLOPS_DATA_S3_ACCESS_KEY",
        "EXAMLOPS_DATA_S3_SECRET_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "MLFLOW_S3_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXAMLOPS_DATA_BUCKET", "examlops-data")
    monkeypatch.setenv("EXAMLOPS_DATA_S3_ENDPOINT", "http://minio:9000")
    return monkeypatch


def test_store_from_env_builds_s3_through_pyarrow(store_env):
    store_env.setenv("EXAMLOPS_DATA_S3_ACCESS_KEY", _KEY)
    store_env.setenv("EXAMLOPS_DATA_S3_SECRET_KEY", _SECRET)
    store = st.store_from_env()
    assert store.root == "examlops-data/dataplane"
    opts = _opts(store.fs)
    assert opts["endpoint_override"] == "minio:9000" and opts["scheme"] == "http"
    assert opts["access_key"] == _KEY and opts["region"] == "us-east-1"
    assert opts["anonymous"] is False


def test_store_from_env_without_keys_uses_the_default_credential_chain(store_env):
    """The platform store is operator-trusted: with no explicit keys it is NOT anonymous, so an
    instance role / IRSA / ``~/.aws`` identity can serve it — unlike a files source."""
    opts = _opts(st.store_from_env().fs)
    assert opts["anonymous"] is False
    assert not opts["access_key"] and not opts["secret_key"]


def test_the_platform_store_endpoint_is_not_egress_checked(store_env, monkeypatch):
    """``EXAMLOPS_DATA_S3_ENDPOINT`` is operator configuration: an internal name is expected."""
    looked_up = _fake_dns(monkeypatch, "127.0.0.1")
    store = st.store_from_env()
    assert _opts(store.fs)["endpoint_override"] == "minio:9000"
    assert looked_up == []


# --- the files connector over the wrapper ----------------------------------------------------


@pytest.fixture
def fake_bucket(tmp_path, monkeypatch):
    """Serve ``s3://bucket/...`` from ``tmp_path`` by swapping only the pyarrow S3 constructor.

    ``SubTreeFileSystem`` reports ``type_name == "subtree"``, so the connector's own
    ``_refuse_unsafe_fs`` check still runs for real (it would refuse a ``local`` filesystem).
    The ``minio`` endpoint the connection names is allow-listed and resolves (fake DNS) to a
    private address, so the egress guard runs for real too.
    """
    seen: list[dict] = []

    def fake_arrow_s3(**kwargs):
        seen.append(kwargs)
        return pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())

    monkeypatch.setattr(s3mod, "_arrow_s3", fake_arrow_s3)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "minio")
    _fake_dns(monkeypatch, "172.18.0.5")
    drop = tmp_path / "bucket" / "drop"
    drop.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"a": 1}, {"a": 2}]), drop / "2026-01.parquet")
    pq.write_table(pa.Table.from_pylist([{"a": 3}]), drop / "2026-02.parquet")
    return seen


_CONN = {"access_key": _KEY, "secret": _SECRET, "endpoint": "http://minio:9000"}


def test_files_connector_reads_s3_through_pyarrow(fake_bucket):
    spec = {"url": "s3://bucket/drop/*.parquet", "table": "t"}
    batches = list(FilesConnector().read(_CONN, spec, None, Limits()))
    assert sum(b.batch.num_rows for b in batches) == 3
    assert {b.table for b in batches} == {"t"}
    kw = fake_bucket[-1]
    assert kw["scheme"] == "http"
    assert kw["access_key"] == _KEY and kw["secret_key"] == _SECRET
    assert "anonymous" not in kw


def test_files_connector_incremental_s3_skips_unchanged_objects(fake_bucket):
    spec = {"url": "s3://bucket/drop"}
    first = list(FilesConnector().read(_CONN, spec, None, Limits()))
    since = first[-1].watermark
    assert list(FilesConnector().read(_CONN, spec, since, Limits())) == []


def test_files_connector_probe_and_discover_s3(fake_bucket):
    conn = FilesConnector()
    assert conn.probe(_CONN, {"url": "s3://bucket/drop"}).ok
    assert not conn.probe(_CONN, {"url": "s3://bucket/missing"}).ok
    tables = conn.discover(_CONN, {"url": "s3://bucket/drop"})
    assert [t.name for t in tables] == ["2026-01", "2026-02"]


def test_a_source_without_credentials_reads_anonymously(fake_bucket):
    """Sources never borrow the service's own identity (``AWS_*``, instance role)."""
    conn = {"endpoint": "http://minio:9000"}
    batches = list(FilesConnector().read(conn, {"url": "s3://bucket/drop"}, None, Limits()))
    assert sum(b.batch.num_rows for b in batches) == 3
    assert fake_bucket[-1]["anonymous"] is True
    assert "access_key" not in fake_bucket[-1]


def test_the_connections_region_reaches_pyarrow(fake_bucket):
    conn = {**_CONN, "region": "eu-west-1"}
    list(FilesConnector().read(conn, {"url": "s3://bucket/drop"}, None, Limits()))
    assert fake_bucket[-1]["region"] == "eu-west-1"


@pytest.mark.parametrize("bad", ["EU_WEST_1", "eu-west-1; x", "", " "])
def test_a_malformed_connection_region_is_refused(bad):
    if not bad.strip():  # blank means "unset" — not an error
        assert "region" not in _storage_options("s3", {"region": bad}, "s3://b/k")
        return
    with pytest.raises(SpecError, match="region"):
        _storage_options("s3", {"region": bad}, "s3://b/k")


def test_s3_options_never_take_an_unchecked_endpoint_from_the_connection():
    """Fail closed: a connection endpoint reaches pyarrow only through ``_guard_s3_endpoint``."""
    with pytest.raises(EgressDenied, match="egress"):
        _storage_options("s3", {"endpoint": "http://minio:9000"}, "s3://b/k")


def test_glob_detail_avoids_one_info_call_per_match(fake_bucket, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = ArrowFSWrapper.info

    def counting(self, path, **kw):
        calls["n"] += 1
        return real(self, path, **kw)

    monkeypatch.setattr(ArrowFSWrapper, "info", counting)
    spec = {"url": "s3://bucket/drop/*.parquet"}
    FilesConnector().discover(_CONN, spec)
    with_two = calls["n"]
    for i in range(3, 8):
        pq.write_table(pa.Table.from_pylist([{"a": i}]), tmp_path / "bucket/drop" / f"x{i}.parquet")
    calls["n"] = 0
    assert len(FilesConnector().discover(_CONN, spec)) == 7
    assert calls["n"] == with_two


# --- the files connector's S3 endpoint egress guard (SSRF) -----------------------------------


@contextlib.contextmanager
def _metadata_like_server():
    """A plain HTTP server that answers every request with a JSON 'credential' document."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            hits.append(f"{self.command} {self.path}")
            body = b'{"AccessKeyId": "internal", "Token": "internal"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Last-Modified", "Thu, 10 Sep 2026 00:00:00 GMT")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = _answer

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1], hits
    finally:
        httpd.shutdown()
        httpd.server_close()


_SSRF_SPEC = {"url": "s3://latest/meta-data/creds", "format": "json"}


def test_a_loopback_s3_endpoint_is_refused_by_read():
    with _metadata_like_server() as (port, hits):
        conn = {"endpoint": f"http://127.0.0.1:{port}"}
        with pytest.raises(EgressDenied):
            list(FilesConnector().read(conn, _SSRF_SPEC, None, Limits()))
        assert hits == []


def test_a_loopback_s3_endpoint_is_refused_by_probe():
    with _metadata_like_server() as (port, hits):
        probe = FilesConnector().probe({"endpoint": f"http://127.0.0.1:{port}"}, _SSRF_SPEC)
        assert probe.ok is False and "EgressDenied" in probe.detail
        assert hits == []


def test_an_internal_compose_name_is_refused_by_read(fake_bucket, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS")
    with pytest.raises(EgressDenied, match="platform-internal"):
        list(FilesConnector().read(_CONN, {"url": "s3://bucket/drop"}, None, Limits()))
    assert fake_bucket == []  # no S3 filesystem was ever built


def test_an_internal_compose_name_is_refused_by_probe(fake_bucket, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS")
    probe = FilesConnector().probe(_CONN, {"url": "s3://bucket/drop"})
    assert probe.ok is False and "platform-internal" in probe.detail
    assert fake_bucket == []


def test_an_allow_listed_http_endpoint_is_admitted_and_pinned_by_read(fake_bucket):
    batches = list(FilesConnector().read(_CONN, {"url": "s3://bucket/drop"}, None, Limits()))
    assert sum(b.batch.num_rows for b in batches) == 3
    # pinned to the address the guard approved: pyarrow never re-resolves `minio` (rebinding)
    assert fake_bucket[-1]["endpoint_override"] == "172.18.0.5:9000"
    assert fake_bucket[-1]["scheme"] == "http"


def test_an_allow_listed_http_endpoint_is_admitted_and_pinned_by_probe(fake_bucket):
    assert FilesConnector().probe(_CONN, {"url": "s3://bucket/drop"}).ok
    assert fake_bucket[-1]["endpoint_override"] == "172.18.0.5:9000"


def test_an_allow_listed_https_endpoint_keeps_its_hostname_for_tls(fake_bucket):
    conn = {**_CONN, "endpoint": "https://minio:9443"}
    assert FilesConnector().probe(conn, {"url": "s3://bucket/drop"}).ok
    assert fake_bucket[-1]["endpoint_override"] == "minio:9443"
    assert fake_bucket[-1]["scheme"] == "https"


def test_the_guard_pins_ipv6_with_brackets_and_default_ports(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "minio")
    _fake_dns(monkeypatch, "fd00::5")
    assert _guard_s3_endpoint({"endpoint": "http://minio"}) == "http://[fd00::5]:80"
    assert _guard_s3_endpoint({"endpoint_url": " http://minio:9000 "}) == "http://[fd00::5]:9000"


def test_the_guard_checks_the_https_default_port(monkeypatch):
    ports: list[int] = []

    def resolver(host, port, **_kw):
        ports.append(port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setitem(safety.check_address.__kwdefaults__, "resolver", resolver)
    assert _guard_s3_endpoint({"endpoint": "https://s3.example.org"}) == "https://s3.example.org"
    assert ports == [443]


def test_no_endpoint_means_the_public_aws_endpoint_and_no_check(monkeypatch):
    looked_up = _fake_dns(monkeypatch, "127.0.0.1")
    assert _guard_s3_endpoint({"access_key": _KEY}) is None
    assert _guard_s3_endpoint(None) is None
    assert looked_up == []


@pytest.mark.parametrize("bad", ["http://minio:abc", "http://minio:99999", "ftp://minio"])
def test_the_guard_refuses_a_malformed_endpoint(bad):
    with pytest.raises(SpecError, match="endpoint"):
        _guard_s3_endpoint({"endpoint": bad})


# --- packaging ------------------------------------------------------------------------------


def test_no_dataplane_extra_depends_on_s3fs():
    """s3fs pins aiobotocore, which caps botocore below the workspace's boto3 floor: an extra
    that pulls it in can never be locked together with the root (task 22b)."""
    extras = tomllib.loads((REPO_ROOT / "platform" / "cli" / "pyproject.toml").read_text())[
        "project"
    ]["optional-dependencies"]
    dataplane = {k: v for k, v in extras.items() if k.startswith("dataplane")}
    assert "dataplane-files" in dataplane
    for name, deps in dataplane.items():
        for dep in deps:
            assert not dep.lower().startswith(("s3fs", "aiobotocore")), f"{name}: {dep}"
    assert any(d.startswith("pyarrow") for d in dataplane["dataplane-files"])
