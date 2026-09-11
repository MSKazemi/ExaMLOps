"""ADR 0130 — a Named Connection's credential goes only where the connection itself points.

Final whole-branch review, fix group FA. Each block is a reproduced finding:

- **C1** a source spec (or a remote server) chose where a connection's secret was sent: the files
  connector put ``Authorization: Bearer <secret>`` on a request to whatever host ``spec.url``
  named, for any connection kind (an S3 secret key reached an arbitrary http host as a bearer
  token), and the Zenodo connector sent its token to ``spec.base_url`` and to every file link the
  record JSON named.
- **I7** sftp trusted any host key (fsspec's ``AutoAddPolicy``) and never egress-checked the host,
  so the connection's password went to an unauthenticated — possibly internal — host.
- **I8** the spec credential check missed ``Authorization``, ``X-API-Key``, ``Cookie``, ``auth``
  and ``credential``, so ``spec.headers: {"X-API-Key": ...}`` was stored in plaintext.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import socket
import sys
import threading
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import dataplane as dpl  # noqa: E402
from examlops.dataplane.connectors import zenodo  # noqa: E402
from examlops.dataplane.connectors.files import FilesConnector  # noqa: E402
from examlops.dataplane.connectors.zenodo import ZenodoConnector  # noqa: E402
from examlops.dataplane.pull import _reject_secret_keys  # noqa: E402
from examlops.dataplane.types import EgressDenied, Limits, SpecError  # noqa: E402

_S3_SECRET = "S3-SECRET-KEY-VALUE"
_ZENODO_TOKEN = "ZENODO-TOKEN-VALUE"
_BEARER = "URI-BEARER-VALUE"


@contextlib.contextmanager
def _server(*, record_link_port: int | None = None):
    """A loopback HTTP server recording ``(path, Authorization)`` for every request it gets.

    ``/api/records/1`` answers a Zenodo record whose one file link points at
    ``record_link_port`` (another server — a foreign origin) or, when ``None``, at itself.
    Everything else answers a two-line CSV body.
    """
    seen: list[tuple[str, str | None]] = []

    class _H(http.server.BaseHTTPRequestHandler):
        def _body(self) -> bytes:
            if self.path.startswith("/api/records/1"):
                port = record_link_port or self.server.server_address[1]
                csv = b"a,b\n1,2\n"
                return json.dumps(
                    {
                        "id": 1,
                        "files": [
                            {
                                "key": "a.csv",
                                "checksum": "md5:" + hashlib.md5(csv).hexdigest(),  # noqa: S324
                                "links": {"self": f"http://127.0.0.1:{port}/elsewhere/a.csv"},
                            }
                        ],
                    }
                ).encode()
            return b"a,b\n1,2\n"

        def _respond(self, include_body: bool) -> None:
            seen.append((self.path, self.headers.get("Authorization")))
            body = self._body()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(False)

        def log_message(self, *a: object) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1], seen
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def _auth_headers(seen: list[tuple[str, str | None]]) -> list[str]:
    return [a for _, a in seen if a]


@pytest.fixture
def loopback(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.1")


# ── C1: the reviewer's repro, as a regression test ────────────────────────────────────────────


def test_repro_no_secret_ever_reaches_a_foreign_origin_server(loopback):
    """The final review's `leak.py`, verbatim in shape: an S3-kind connection's secret key went to
    an arbitrary http host as a bearer token, and a Zenodo token went to a spec-chosen base_url."""
    with _server() as (port, seen):
        conn = {"kind": "s3", "secret": _S3_SECRET, "access_key": "AK"}
        with pytest.raises(SpecError):
            list(
                FilesConnector().read(
                    conn, {"url": f"http://127.0.0.1:{port}/x.csv"}, None, Limits()
                )
            )
        with pytest.raises(SpecError):
            list(
                ZenodoConnector().read(
                    {"kind": "zenodo", "secret": _ZENODO_TOKEN},
                    {"record": 1, "base_url": f"http://127.0.0.1:{port}"},
                    None,
                    Limits(),
                )
            )
    assert seen == []  # refused before any request was made
    assert not any(_S3_SECRET in a or _ZENODO_TOKEN in a for a in _auth_headers(seen))


# ── C1 (a): files over http(s) — auth only to the connection's own origin ─────────────────────


def test_uri_connection_sends_its_bearer_to_its_own_origin(loopback):
    with _server() as (port, seen):
        conn = {"kind": "uri", "uri": f"http://127.0.0.1:{port}/exports/", "secret": _BEARER}
        batches = list(
            FilesConnector().read(conn, {"url": f"http://127.0.0.1:{port}/x.csv"}, None, Limits())
        )
    assert sum(b.batch.num_rows for b in batches) == 1
    assert _auth_headers(seen) == [f"Bearer {_BEARER}"]


def test_uri_connection_sends_nothing_to_a_foreign_origin(loopback):
    """Same scheme and host, a different port: a different origin, so no credential."""
    with _server() as (own_port, own_seen), _server() as (other_port, other_seen):
        conn = {"kind": "uri", "uri": f"http://127.0.0.1:{own_port}", "secret": _BEARER}
        spec = {"url": f"http://127.0.0.1:{other_port}/x.csv"}
        batches = list(FilesConnector().read(conn, spec, None, Limits()))
    assert sum(b.batch.num_rows for b in batches) == 1  # the read itself still works, anonymously
    assert len(other_seen) == 1
    assert _auth_headers(other_seen) == []
    assert own_seen == []


def test_connection_without_an_origin_sends_no_credential(loopback):
    """A connection that configures no ``uri``/``url``/``base_url`` has no origin to scope its
    secret to — the spec URL must not become that origin by default."""
    with _server() as (port, seen):
        spec = {"url": f"http://127.0.0.1:{port}/x.csv"}
        list(FilesConnector().read({"kind": "uri", "secret": _BEARER}, spec, None, Limits()))
        list(FilesConnector().read({"secret": _BEARER}, spec, None, Limits()))
    assert len(seen) == 2
    assert _auth_headers(seen) == []


def test_probe_and_discover_follow_the_same_origin_rule(loopback):
    with _server() as (own_port, own_seen), _server() as (other_port, other_seen):
        conn = {"kind": "uri", "uri": f"http://127.0.0.1:{own_port}", "secret": _BEARER}
        own = {"url": f"http://127.0.0.1:{own_port}/x.csv"}
        other = {"url": f"http://127.0.0.1:{other_port}/x.csv"}
        assert FilesConnector().probe(conn, own).ok
        assert FilesConnector().probe(conn, other).ok
        FilesConnector().discover(conn, own)
        FilesConnector().discover(conn, other)
        assert FilesConnector().probe(conn, None).ok  # no spec: probes the connection's own uri
    assert _auth_headers(own_seen) == [f"Bearer {_BEARER}"] * 3
    assert len(other_seen) == 2
    assert _auth_headers(other_seen) == []


# ── C1 (b): the connection kind decides which URL schemes a source may use ────────────────────


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        ("s3", "https://example.org/x.csv"),
        ("s3", "http://example.org/x.csv"),
        ("s3", "sftp://example.org/x.csv"),
        ("s3", "gs://bucket/x.csv"),
        ("fs", "https://example.org/x.csv"),
        ("fs", "s3://bucket/x.csv"),
        ("uri", "s3://bucket/x.csv"),
        ("uri", "sftp://example.org/x.csv"),
        ("uri", "file:///tmp/x.csv"),
        ("kafka", "https://example.org/x.csv"),  # a kind this connector does not accept at all
    ],
)
def test_a_scheme_that_does_not_match_the_connection_kind_is_refused(kind, url, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    conn = {"kind": kind, "secret": "never-sent"}
    with pytest.raises(SpecError, match="kind"):
        FilesConnector()._check_url(url, conn)
    with pytest.raises(SpecError, match="kind"):
        list(FilesConnector().read(conn, {"url": url}, None, Limits()))
    with pytest.raises(SpecError, match="kind"):
        FilesConnector().discover(conn, {"url": url})
    probe = FilesConnector().probe(conn, {"url": url})
    assert not probe.ok
    assert "never-sent" not in probe.detail


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        ("s3", "s3://bucket/x.parquet"),
        ("fs", "gs://bucket/x.parquet"),
        ("fs", "abfs://container/x.parquet"),
        ("fs", "sftp://example.org/x.parquet"),
        ("fs", "hdfs://namenode/x.parquet"),
        ("fs", "file:///tmp/x.parquet"),
        ("uri", "https://example.org/x.csv"),
        ("uri", "http://example.org/x.csv"),
    ],
)
def test_a_scheme_matching_the_connection_kind_is_accepted(kind, url, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "1")
    assert FilesConnector()._check_url(url, {"kind": kind}) == url.split("://", 1)[0]


def test_the_kind_matrix_covers_exactly_the_kinds_the_connector_accepts():
    from examlops.dataplane.connectors import files

    assert set(files._KIND_SCHEMES) == set(FilesConnector.connection_kinds)
    covered = set().union(*files._KIND_SCHEMES.values())
    assert covered == files._SCHEMES  # every scheme reachable through exactly one kind
    assert sum(len(v) for v in files._KIND_SCHEMES.values()) == len(covered)


def test_a_source_without_a_connection_may_use_any_scheme():
    assert FilesConnector()._check_url("https://example.org/x.csv", None) == "https"
    assert FilesConnector()._check_url("s3://bucket/x.csv", None) == "s3"


# ── C1 (c): Zenodo — the token goes only to the connection's own server ────────────────────────


def _parquet_bytes() -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{"job": 1}, {"job": 2}]), buf)
    return buf.getvalue()


@pytest.fixture
def zenodo_api(monkeypatch):
    """A recorded Zenodo API; ``state["link"]`` is the file link the record JSON hands out."""
    blob = _parquet_bytes()
    state: dict = {
        "link": "https://zenodo.org/api/records/7/files/jobs.parquet/content",
        "requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append((str(request.url), request.headers.get("Authorization")))
        if request.url.path == "/api/records/7":
            record = {
                "id": 7,
                "revision": 1,
                "files": [
                    {
                        "key": "jobs.parquet",
                        "checksum": "md5:" + hashlib.md5(blob).hexdigest(),  # noqa: S324
                        "links": {"content": state["link"]},
                    }
                ],
            }
            return httpx.Response(200, json=record)
        return httpx.Response(200, content=blob)

    monkeypatch.setattr(
        zenodo,
        "_client_factory",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    return state


_ZCONN = {"kind": "zenodo", "secret": _ZENODO_TOKEN}


def _zread(conn, spec):
    return list(ZenodoConnector().read(conn, {"record": 7, **spec}, None, Limits()))


def test_zenodo_token_reaches_its_own_api_and_same_origin_file_links(zenodo_api):
    batches = _zread(_ZCONN, {})
    assert sum(b.batch.num_rows for b in batches) == 2
    assert [a for _, a in zenodo_api["requests"]] == [f"Bearer {_ZENODO_TOKEN}"] * 2


def test_zenodo_refuses_a_foreign_spec_base_url_when_the_connection_has_a_token(zenodo_api):
    for bad in ("https://evil.example", "http://zenodo.org", "https://zenodo.org:8443"):
        with pytest.raises(SpecError, match="base_url"):
            _zread(_ZCONN, {"base_url": bad})
        with pytest.raises(SpecError, match="base_url"):
            ZenodoConnector().discover(_ZCONN, {"record": 7, "base_url": bad})
        probe = ZenodoConnector().probe(_ZCONN, {"record": 7, "base_url": bad})
        assert not probe.ok
        assert _ZENODO_TOKEN not in probe.detail
    assert zenodo_api["requests"] == []


def test_zenodo_spec_base_url_on_the_connections_own_origin_is_accepted(zenodo_api):
    _zread(_ZCONN, {"base_url": "https://ZENODO.org/"})
    own = {**_ZCONN, "base_url": "https://sandbox.zenodo.org"}
    zenodo_api["link"] = "https://sandbox.zenodo.org/api/records/7/files/jobs.parquet/content"
    _zread(own, {"base_url": "https://sandbox.zenodo.org:443"})
    with pytest.raises(SpecError, match="base_url"):
        _zread(own, {"base_url": "https://zenodo.org"})  # the default is not this conn's origin
    assert all(a == f"Bearer {_ZENODO_TOKEN}" for _, a in zenodo_api["requests"])


def test_zenodo_without_a_token_may_use_any_base_url(zenodo_api):
    zenodo_api["link"] = "https://mirror.example/api/records/7/files/jobs.parquet/content"
    _zread(None, {"base_url": "https://mirror.example"})
    _zread({"kind": "zenodo"}, {"base_url": "https://mirror.example"})
    assert len(zenodo_api["requests"]) == 4
    assert all(a is None for _, a in zenodo_api["requests"])


@pytest.mark.parametrize(
    "link",
    [
        "https://files.example/jobs.parquet",  # another host entirely
        "http://zenodo.org/api/records/7/files/jobs.parquet/content",  # a downgraded scheme
        "https://zenodo.org:8443/api/records/7/files/jobs.parquet/content",  # another port
    ],
)
def test_zenodo_file_link_on_a_foreign_origin_gets_no_token(zenodo_api, link):
    zenodo_api["link"] = link
    batches = _zread(_ZCONN, {})
    assert sum(b.batch.num_rows for b in batches) == 2
    api, download = zenodo_api["requests"]
    assert api[1] == f"Bearer {_ZENODO_TOKEN}"
    assert download == (link, None)


def test_zenodo_foreign_file_link_over_real_http_carries_no_token(loopback, monkeypatch):
    """The repro's second half end to end over sockets: the API server hands out a file link on
    another server; that server must see no Authorization at all."""
    monkeypatch.setattr(zenodo, "_client_factory", zenodo.guarded_client)
    with _server() as (link_port, link_seen):
        with _server(record_link_port=link_port) as (api_port, api_seen):
            conn = {**_ZCONN, "base_url": f"http://127.0.0.1:{api_port}"}
            batches = list(ZenodoConnector().read(conn, {"record": 1}, None, Limits()))
    assert sum(b.batch.num_rows for b in batches) == 1
    assert _auth_headers(api_seen) == [f"Bearer {_ZENODO_TOKEN}"]
    assert len(link_seen) == 1
    assert _auth_headers(link_seen) == []


# ── I7: sftp verifies host keys and is egress-checked ─────────────────────────────────────────


class _FakeSFTP:
    def stat(self, path):
        raise FileNotFoundError(path)

    def listdir_attr(self, path):
        raise FileNotFoundError(path)


class _FakeSSHClient:
    """Stands in for ``paramiko.SSHClient``; records everything a connect would do."""

    instances: list[_FakeSSHClient] = []

    def __init__(self) -> None:
        self.policy = None
        self.host_key_files: list[str | None] = []
        self.connected: tuple[str, dict] | None = None
        _FakeSSHClient.instances.append(self)

    def load_system_host_keys(self, filename=None):
        if filename is not None and not Path(filename).is_file():
            raise OSError(filename)
        self.host_key_files.append(filename)

    def load_host_keys(self, filename):
        self.host_key_files.append(filename)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, hostname, **kwargs):
        self.connected = (hostname, kwargs)

    def open_sftp(self):
        return _FakeSFTP()

    def close(self):
        pass


def _clear_sftp_caches() -> None:
    from fsspec.implementations.sftp import SFTPFileSystem

    for cls in (SFTPFileSystem, *SFTPFileSystem.__subclasses__()):
        cls.clear_instance_cache()


@pytest.fixture
def fake_ssh(monkeypatch):
    import paramiko

    _FakeSSHClient.instances = []
    sockets: list[tuple[tuple[str, int], object]] = []

    class _Sock:
        def close(self) -> None:
            pass

    def _create_connection(address, timeout=None, *a, **kw):
        sockets.append((address, timeout))
        return _Sock()

    monkeypatch.setattr(paramiko, "SSHClient", _FakeSSHClient)
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    monkeypatch.delenv("EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS", raising=False)
    _clear_sftp_caches()
    yield {"clients": _FakeSSHClient.instances, "sockets": sockets}
    _clear_sftp_caches()


def _connected(fake_ssh) -> list[_FakeSSHClient]:
    return [c for c in fake_ssh["clients"] if c.connected is not None]


def test_sftp_rejects_unknown_host_keys_by_default(fake_ssh, loopback):
    import paramiko

    probe = FilesConnector().probe(None, {"url": "sftp://127.0.0.1/drops/x.csv"})
    assert probe.detail == "path not found"
    (client,) = _connected(fake_ssh)
    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert None in client.host_key_files  # the system/user known_hosts were loaded


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_sftp_auto_adds_host_keys_only_with_the_opt_in(fake_ssh, loopback, monkeypatch, value):
    import paramiko

    monkeypatch.setenv("EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS", value)
    FilesConnector().probe(None, {"url": "sftp://127.0.0.1/drops/x.csv"})
    (client,) = _connected(fake_ssh)
    assert isinstance(client.policy, paramiko.AutoAddPolicy)


def test_sftp_opt_in_off_values_keep_reject_policy(fake_ssh, loopback, monkeypatch):
    import paramiko

    monkeypatch.setenv("EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS", "0")
    FilesConnector().probe(None, {"url": "sftp://127.0.0.1/drops/x.csv"})
    (client,) = _connected(fake_ssh)
    assert isinstance(client.policy, paramiko.RejectPolicy)


def test_sftp_loads_the_dataplane_known_hosts_file(fake_ssh, loopback, monkeypatch, tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("")
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS", str(known))
    FilesConnector().probe(None, {"url": "sftp://127.0.0.1/drops/x.csv"})
    (client,) = _connected(fake_ssh)
    assert str(known) in client.host_key_files


def test_sftp_refuses_an_unreadable_known_hosts_file(fake_ssh, loopback, monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS", str(tmp_path / "absent"))
    with pytest.raises(SpecError, match="EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS"):
        FilesConnector().discover(None, {"url": "sftp://127.0.0.1/drops/x.csv"})
    assert _connected(fake_ssh) == []


def test_sftp_host_is_egress_checked_before_connecting(fake_ssh, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    for url in ("sftp://127.0.0.1/x.csv", "sftp://localhost/x.csv", "sftp://minio/x.csv"):
        with pytest.raises(EgressDenied):
            FilesConnector().discover(None, {"url": url})
    assert _connected(fake_ssh) == []
    assert fake_ssh["sockets"] == []


def test_sftp_connects_to_the_checked_address_and_verifies_the_name(fake_ssh, monkeypatch):
    """The TCP connection goes to the address the egress check approved (no second DNS lookup);
    paramiko still gets the *name*, which is what known_hosts entries are keyed on. The service's
    own SSH agent and keys are never offered to a source's host."""
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "localhost")
    FilesConnector().probe(None, {"url": "sftp://localhost:2222/x.csv"})
    (client,) = _connected(fake_ssh)
    hostname, kwargs = client.connected
    assert hostname == "localhost"
    assert kwargs["port"] == 2222
    assert kwargs["sock"] is not None
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False
    ((address, _timeout),) = fake_ssh["sockets"]
    assert address[0] in {"127.0.0.1", "::1"}
    assert address[1] == 2222


def test_sftp_with_no_host_is_refused(fake_ssh, loopback):
    for url in ("sftp:///x.csv", "sftp://:22/x.csv"):
        with pytest.raises(SpecError, match="host"):
            FilesConnector().discover(None, {"url": url})
        with pytest.raises(SpecError, match="host"):
            FilesConnector().discover({"kind": "fs", "username": "u"}, {"url": url})
    assert _connected(fake_ssh) == []


def test_sftp_without_a_url_host_uses_the_connections_host(fake_ssh, loopback):
    conn = {"kind": "fs", "host": "127.0.0.1", "username": "u", "secret": "PW"}
    FilesConnector().probe(conn, {"url": "sftp:///drops/x.csv"})
    (client,) = _connected(fake_ssh)
    assert client.connected[0] == "127.0.0.1"
    assert client.connected[1]["password"] == "PW"


@pytest.mark.parametrize(
    ("conn", "url"),
    [
        # the spec names another host than the connection's own
        ({"host": "127.0.0.1"}, "sftp://localhost/x.csv"),
        # same host, another port — another sshd, possibly another user's
        ({"host": "127.0.0.1"}, "sftp://127.0.0.1:2222/x.csv"),
        ({"host": "127.0.0.1", "port": 2222}, "sftp://127.0.0.1:22/x.csv"),
        # a password-bearing connection that names no host lets the spec choose: refused
        ({}, "sftp://127.0.0.1/x.csv"),
    ],
)
def test_sftp_password_never_goes_to_a_host_the_connection_does_not_name(
    fake_ssh, monkeypatch, conn, url
):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.1,localhost")
    full = {"kind": "fs", "username": "u", "secret": "SFTP-PW", **conn}
    with pytest.raises(SpecError, match="host"):
        FilesConnector().discover(full, {"url": url})
    probe = FilesConnector().probe(full, {"url": url})
    assert not probe.ok
    assert "SFTP-PW" not in probe.detail
    assert _connected(fake_ssh) == []


def test_sftp_connection_port_is_used_when_the_url_has_none(fake_ssh, loopback):
    conn = {"kind": "fs", "host": "127.0.0.1", "port": 2222, "username": "u", "secret": "PW"}
    FilesConnector().probe(conn, {"url": "sftp:///x.csv"})
    (client,) = _connected(fake_ssh)
    assert client.connected[1]["port"] == 2222


# ── I8: credential-shaped spec keys are refused, however they are spelled ─────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "X-API-Key",
        "x_api_key",
        "XApiKey",
        "api-key",
        "Cookie",
        "Set-Cookie",
        "auth",
        "Auth",
        "x-auth",
        "basic_auth",
        "auth_header",
        "credential",
        "credentials",
        "Bearer",
        "X-Auth-Token",
        "token",
        "secret",
        "client-secret",
        "password",
        "passwd",
        "passphrase",
        "private-key",
        "privateKey",
        "access-key",
    ],
)
def test_credential_shaped_keys_are_refused_at_any_depth(key):
    value = "CREDENTIAL-VALUE-123"
    for spec, where in (
        ({key: value}, f"spec.{key}"),
        ({"headers": {key: value}}, f"spec.headers.{key}"),
        ({"params": [{"q": 1}, {key: value}]}, f"spec.params[1].{key}"),
    ):
        with pytest.raises(SpecError) as info:
            _reject_secret_keys(spec)
        message = str(info.value)
        assert where in message  # names the key …
        assert value not in message  # … never the value


@pytest.mark.parametrize(
    "key",
    ["url", "headers", "Accept", "User-Agent", "author", "authority", "records_path", "table"],
)
def test_ordinary_keys_are_not_mistaken_for_credentials(key):
    assert _reject_secret_keys({key: "v", "headers": {key: "v"}}) is None


def test_define_source_refuses_an_api_key_header_before_storing_anything():
    from examlops.data import dataplane as catalog

    spec = {"url": "https://api.example/x.csv", "headers": {"X-API-Key": "PLAINTEXT-KEY"}}
    with pytest.raises(SpecError, match="X-API-Key") as info:
        dpl.define_source("hdr", "files", spec=spec)
    assert "PLAINTEXT-KEY" not in str(info.value)
    assert catalog.get_source("hdr", "") is None


# --- URL query credentials: exact names only (final-review FB, controller follow-up) ------------


@pytest.mark.parametrize(
    "query",
    ["sort_key=a", "country_code=IT", "page_token=abc", "q=pm100", "keyword=x", "session_window=5"],
)
def test_ordinary_query_parameters_are_not_mistaken_for_credentials(query):
    from examlops.dataplane.pull import _url_query_credential

    assert _url_query_credential(f"https://api.example.org/v1/items?{query}") is None


@pytest.mark.parametrize(
    "query, name",
    [
        ("X-Amz-Signature=zz", "X-Amz-Signature"),
        ("sv=2024&sig=zz", "sig"),
        ("api_key=zz", "api_key"),
        ("access_token=zz", "access_token"),
        ("code=zz", "code"),
    ],
)
def test_credential_query_parameters_are_refused_by_name(query, name):
    from examlops.dataplane.pull import _url_query_credential

    assert _url_query_credential(f"https://api.example.org/v1/items?{query}") == name
