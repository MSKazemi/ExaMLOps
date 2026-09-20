"""ADR 0130 §10 — egress policy, DNS-rebinding-proof HTTP, redaction, name validation."""

from __future__ import annotations

import contextlib
import http.server
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane import safety  # noqa: E402
from examlops.dataplane.types import EgressDenied, SpecError  # noqa: E402


def _resolver(ip: str):
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


def _make_handler(*, redirect_to: str | None = None, captured: list[dict[str, str]] | None = None):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if captured is not None:
                captured.append(dict(self.headers.items()))
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:  # silence per-request stderr noise
            pass

    return _Handler


@contextlib.contextmanager
def _local_server(*, redirect_to: str | None = None, captured: list[dict[str, str]] | None = None):
    """A stdlib HTTP server on an OS-assigned loopback port — never one of conftest's live ports."""
    httpd = http.server.HTTPServer(
        ("127.0.0.1", 0), _make_handler(redirect_to=redirect_to, captured=captured)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.9",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",  # RFC 6598 shared/CGNAT space — not private, not reserved, but not global
        "100.100.100.200",  # Alibaba Cloud's metadata endpoint lives in that same /10
    ],
)
def test_non_public_addresses_are_denied(ip):
    with pytest.raises(EgressDenied, match="non-public"):
        safety.check_address("example.org", 443, resolver=_resolver(ip))


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "mlflow",
        "minio",
        "minio-init",
        "postgres",
        "control-plane",
        "nats",
        "vllm",
        "dataplane-bus-bridge",
        "backup",
        "mlflow.",  # trailing FQDN dot
        "MLFLOW",  # case
        "docker-socket-proxy.",
        "examlops-mlflow",  # compose `container_name:` prefix
        "examlops-minio",
    ],
)
def test_stack_service_names_are_denied(host):
    with pytest.raises(EgressDenied, match="platform-internal"):
        safety.check_address(host, 80, resolver=_resolver("93.184.216.34"))


def test_internal_name_bypass_is_closed_even_with_an_overlapping_cidr(monkeypatch):
    """An operator's own CIDR allow-list must not double as an internal-name bypass.

    Before the fix, a variant spelling of a compose service name (trailing dot, container-name
    prefix, or one of the services missing from the denylist) sailed past the name check and then
    passed the IP-level check purely because it happened to resolve inside an allow-listed CIDR
    the operator meant for something else entirely.
    """
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "172.16.0.0/12")
    for host in (
        "mlflow.",
        "docker-socket-proxy.",
        "examlops-mlflow",
        "nats",
        "vllm",
        "dataplane-bus-bridge",
        "backup",
        "minio-init",
    ):
        with pytest.raises(EgressDenied, match="platform-internal"):
            safety.check_address(host, 80, resolver=_resolver("172.18.0.5"))


def test_explicit_hostname_allow_list_still_wins_over_container_prefix(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "examlops-mlflow")
    assert (
        safety.check_address("examlops-mlflow", 5000, resolver=_resolver("172.18.0.5"))
        == "172.18.0.5"
    )


def test_container_prefix_rule_does_not_deny_a_public_multi_label_name():
    """`examlops-docs.example.org` merely starts with the compose prefix; it is not a container."""
    assert (
        safety.check_address("examlops-docs.example.org", 443, resolver=_resolver("93.184.216.34"))
        == "93.184.216.34"
    )


def test_public_address_passes_and_is_returned():
    assert (
        safety.check_address("example.org", 443, resolver=_resolver("93.184.216.34"))
        == "93.184.216.34"
    )


def test_allow_list_admits_hosts_and_cidrs(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "minio, 10.0.0.0/8")
    assert safety.check_address("minio", 9000, resolver=_resolver("172.18.0.5")) == "172.18.0.5"
    assert safety.check_address("db.lab", 5432, resolver=_resolver("10.9.9.9")) == "10.9.9.9"


def test_a_resolution_failure_is_mapped_to_egress_denied_not_a_raw_dns_error():
    """fix KS round 1, Important 3: a caller (sql/kafka) must never see a raw ``socket.gaierror``
    bubble out of ``check_address`` — every failure mode is a ``DataplaneError``."""

    def broken_resolver(host, port, *a, **kw):
        raise socket.gaierror(-2, "Name or service not known")

    with pytest.raises(EgressDenied, match="did not resolve"):
        safety.check_address("broker.invalid", 9092, resolver=broken_resolver)


def test_guarded_client_connects_to_the_checked_ip_not_a_second_lookup(monkeypatch):
    """DNS rebinding: first answer public, second answer loopback. The guard must use the first."""
    answers = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(
        safety.socket, "getaddrinfo", lambda h, p, *a, **k: _resolver(next(answers))(h, p)
    )
    seen: list[str] = []

    class _Inner:
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            seen.append(host)
            raise httpx.ConnectError("stop here")

    backend = safety._GuardedBackend(_Inner())
    with pytest.raises(httpx.ConnectError):
        backend.connect_tcp("example.org", 443)
    assert seen == ["93.184.216.34"]


def test_guarded_client_is_actually_wired():
    """If an httpx upgrade moves the private attributes, fail loudly instead of running unguarded."""
    client = safety.guarded_client()
    assert isinstance(client._transport._pool._network_backend, safety._GuardedBackend)  # type: ignore[attr-defined]
    assert client._trust_env is False  # env proxies would bypass the guard


def test_guarded_client_redirect_headers_override_is_actually_wired():
    """If an httpx upgrade stops calling ``_redirect_headers``, fail loudly."""
    client = safety.guarded_client()
    assert type(client)._redirect_headers is not httpx.Client._redirect_headers


def test_guarded_client_refuses_https_to_http_downgrade_redirect():
    client = safety.guarded_client()
    request = httpx.Request("GET", "https://example.org/start")
    url = httpx.URL("http://example.org/next")
    with pytest.raises(EgressDenied):
        client._redirect_headers(request, url, "GET")


def test_guarded_client_denies_loopback_without_allow_list(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", raising=False)
    with _local_server() as port:
        client = safety.guarded_client(timeout=2.0)
        try:
            with pytest.raises(EgressDenied):
                client.get(f"http://127.0.0.1:{port}/")
        finally:
            client.close()


def test_guarded_client_allows_loopback_when_allow_listed(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    with _local_server() as port:
        client = safety.guarded_client(timeout=2.0)
        try:
            resp = client.get(f"http://127.0.0.1:{port}/")
            assert resp.status_code == 200
        finally:
            client.close()


def test_guarded_client_refuses_redirect_to_a_denied_address(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    with _local_server(redirect_to="http://10.7.8.9/secret") as port:
        client = safety.guarded_client(timeout=2.0)
        try:
            with pytest.raises(EgressDenied):
                client.get(f"http://127.0.0.1:{port}/")
        finally:
            client.close()


def test_guarded_client_ignores_proxy_env_vars(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1/")
    with _local_server() as port:
        client = safety.guarded_client(timeout=2.0)
        try:
            resp = client.get(f"http://127.0.0.1:{port}/")
            assert resp.status_code == 200
        finally:
            client.close()


def test_guarded_client_strips_caller_headers_on_cross_origin_redirect(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "127.0.0.0/8")
    captured: list[dict[str, str]] = []
    with _local_server(captured=captured) as port_b:
        with _local_server(redirect_to=f"http://127.0.0.1:{port_b}/") as port_a:
            client = safety.guarded_client(timeout=2.0, headers={"X-Api-Key": "topsecret"})
            try:
                resp = client.get(f"http://127.0.0.1:{port_a}/")
                assert resp.status_code == 200
            finally:
                client.close()
    assert captured, "the redirect target never received the request"
    assert "x-api-key" not in {k.lower() for k in captured[0]}


def test_redact_strips_userinfo_tokens_and_known_secrets():
    text = "GET https://bob:hunter2@api.x.io/v1?token=abc&page=2 failed; key=s3cr3t"
    out = safety.redact(text, secrets=["s3cr3t"])
    assert "hunter2" not in out and "abc" not in out and "s3cr3t" not in out
    assert "page=2" in out and "api.x.io" in out


@pytest.mark.parametrize(
    "kv",
    [
        "client_secret=zzz1",
        "refresh_token=zzz2",
        "id_token=zzz3",
        "aws_secret_access_key=zzz4",
        "private_key=zzz5",
    ],
)
def test_redact_strips_prefixed_secret_keys(kv):
    key, value = kv.split("=")
    out = safety.redact(f"cfg {kv} end")
    assert value not in out
    assert f"{key}=***" in out


@pytest.mark.parametrize(
    "kv",
    ["password: hunter2", "api_key: abc123"],
)
def test_redact_strips_colon_form_kv(kv):
    key, value = kv.split(": ")
    out = safety.redact(f"cfg {kv} end")
    assert value not in out
    assert f"{key}: ***" in out


@pytest.mark.parametrize(
    "text,leaked",
    [
        ("GET http://dataplane:8080/v1/pull?token=abc failed", "token=abc"),
        ("https://api.x.io:443/v1?api_key=abc", "api_key=abc"),
        ("postgres://h:5432/db?password=hunter2", "password=hunter2"),
        ("Unauthorized: token=abc", "token=abc"),
        ("error: password=hunter2", "password=hunter2"),
        ("url: https://h/p?sig=abc", "sig=abc"),
        ("redirect_uri=https://h/cb?access_token=abc", "access_token=abc"),
    ],
)
def test_redact_does_not_let_a_non_secret_match_swallow_a_nested_secret(text, leaked):
    """A non-secret `k:v`/`k=v` match (e.g. `dataplane:8080/...`) must not consume — and thereby
    hide — a real secret living inside what would otherwise be treated as its "value"."""
    out = safety.redact(text)
    assert leaked not in out
    key = leaked.split("=")[0]
    assert f"{key}=***" in out


def test_redact_non_secret_match_preserves_its_own_surrounding_text():
    out = safety.redact("GET http://dataplane:8080/v1/pull?token=abc failed")
    assert "dataplane:8080/v1/pull" in out
    assert "failed" in out
    assert out == "GET http://dataplane:8080/v1/pull?token=*** failed"


def test_redact_kv_scan_is_linear_on_an_adversarial_input():
    """~70s on 8 KB before the fix (O(n^3) backtracking with no `=`/`:` anywhere in the run)."""
    import time

    adversarial = ("-key" * 25000) + ("a_key" * 20000)
    assert len(adversarial) > 100_000
    start = time.monotonic()
    out = safety.redact(adversarial)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, (
        f"redact() took {elapsed:.3f}s on a {len(adversarial)}-char adversarial input"
    )
    assert out == adversarial  # no `=`/`:` anywhere, so nothing should ever match


def test_redact_scheme_run_is_linear_on_an_adversarial_input():
    """A 100 KB run of scheme-charset letters before `://` was 0.59s with a forward scheme regex
    (one retried start position per character); anchoring on `://` and looking back at most one
    scheme length makes it a single bounded check."""
    import time

    adversarial = ("a" * 100_000) + "://x"  # no `@`, so no userinfo is ever masked
    start = time.monotonic()
    out = safety.redact(adversarial)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, (
        f"redact() took {elapsed:.3f}s on a {len(adversarial)}-char scheme-run input"
    )
    assert out == adversarial


@pytest.mark.parametrize(
    "unit,count",
    [("a:", 50_000), ("x=", 50_000), ("1:", 50_000), ("a=b:", 25_000), ("a:/", 33_000)],
)
def test_redact_kv_scan_is_linear_on_separator_dense_input(unit, count):
    """Separator-dense text with no whitespace was O(n^2): a non-secret key's greedy value was
    matched in full and then — so a secret nested inside it would still be found — rescanned from
    just after the key, so every value was re-matched from nearly every position (~6 s at 100 KB).
    """
    import time

    adversarial = unit * count
    start = time.monotonic()
    out = safety.redact(adversarial)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"redact() took {elapsed:.3f}s on {len(adversarial)} chars of {unit!r}"
    assert out == adversarial  # no key in these is secret-shaped


def test_redact_is_linear_on_a_long_url_path_of_kv_fragments():
    """The realistic form of the same shape: a long URL path of `seg:v` fragments (~4 s before)."""
    import time

    path = "/".join(f"seg{i}:v{i}" for i in range(15_000))
    text = f"GET http://h/{path} failed; token=abc"
    assert len(text) > 150_000
    start = time.monotonic()
    out = safety.redact(text)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"redact() took {elapsed:.3f}s on a {len(text)}-char URL path"
    assert out == f"GET http://h/{path} failed; token=***"


@pytest.mark.parametrize(
    "prefix",
    ["1", "-", "...", "+", "a" * 40, "9" * 32],
)
def test_redact_masks_userinfo_whatever_precedes_the_scheme(prefix):
    """The scheme is the valid run ending at `://`, not a run that must start at a word boundary:
    a digit, `.`, `-` or `+` glued to the scheme, or a letter run longer than a scheme, used to
    make the whole URL unmatchable and leak the password."""
    out = safety.redact(f"GET {prefix}https://bob:hunter2@h/x failed")
    assert "hunter2" not in out
    assert out == f"GET {prefix}https://***@h/x failed"


def test_redact_masks_a_userinfo_password_of_any_length():
    """A 2048-character cap on userinfo (there only to bound regex backtracking) left a longer
    password — a JWT, say — unmasked, and nothing else in `redact` catches `oauth2:<token>@`."""
    token = "eyJ" + "a" * 3000
    out = safety.redact(f"clone https://oauth2:{token}@git.example/repo failed")
    assert token not in out
    assert out == "clone https://***@git.example/repo failed"


@pytest.mark.parametrize(
    "adversarial",
    [
        "a://" * 25_000,
        ("a" * 32 + "://") * 2_857,
        "http://" * 14_000,
        "a://" + "x@" * 50_000,
        ("a://" + "x" * 2_000) * 50,
        ("1" * 33 + "://u@h ") * 2_500,
    ],
    ids=[
        "a-scheme-repeated",
        "long-scheme-repeated",
        "http-repeated",
        "at-dense",
        "long-runs",
        "no-scheme",
    ],
)
def test_redact_userinfo_scan_is_linear(adversarial):
    import time

    start = time.monotonic()
    safety.redact(adversarial)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"redact() took {elapsed:.3f}s on {len(adversarial)} chars"


@pytest.mark.parametrize(
    "header",
    [
        "Authorization: Bearer zzztoken",
        "x-api-key: zzzapikey",
        "Proxy-Authorization: Basic zzzbasic",
    ],
)
def test_redact_strips_header_forms(header):
    name, value = header.split(":", 1)
    out = safety.redact(f"request failed; {header}")
    assert value.strip() not in out
    assert f"{name}: ***" in out


def test_redact_strips_json_password_field():
    out = safety.redact('payload={"password": "hunter2", "user": "bob"}')
    assert "hunter2" not in out
    assert '"password": "***"' in out
    assert '"user": "bob"' in out


@pytest.mark.parametrize(
    "text",
    [
        "fetch https://bob:pa/ss@host.example/path failed",
        "fetch https://bob:p@ss@host.example/path failed",
    ],
)
def test_redact_handles_userinfo_with_slash_or_at_in_password(text):
    out = safety.redact(text)
    assert "pa/ss" not in out
    assert "p@ss" not in out
    assert "host.example/path" in out


def test_redact_masks_explicit_secrets_shorter_than_four_chars():
    out = safety.redact("the pin is ok", secrets=["ok"])
    assert "ok" not in out
    assert safety.redact("nothing to hide", secrets=[""]) == "nothing to hide"


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 129, "spaces no"])
def test_validate_name_rejects_path_tricks(bad):
    with pytest.raises(SpecError):
        safety.validate_name(bad)


def test_local_files_are_off_by_default(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", raising=False)
    assert safety.local_files_allowed() is False


# ── properties verified by reading on 2026-09-14, pinned so they cannot regress ──


def test_the_guarded_backend_refuses_a_unix_socket():
    """A unix socket is egress the address guard cannot judge, and a real SSRF target.

    `_GuardedBackend` **wraps** an inner backend, so this override is the only thing standing
    between a caller and the inner backend's own unix-socket support — `/var/run/docker.sock` is
    the canonical example of what that reaches. The refusal was correct and untested: an httpcore
    upgrade that renamed the method, or a refactor that dropped the override, would silently expose
    the inner one.
    """

    class _Inner:
        def connect_unix_socket(self, *a, **kw):  # pragma: no cover - must never be reached
            raise AssertionError("the inner backend was reached")

    backend = safety._GuardedBackend(_Inner())
    with pytest.raises(safety.EgressDenied):
        backend.connect_unix_socket("/var/run/docker.sock")


def test_the_unix_socket_override_still_matches_the_backend_it_wraps():
    """Anti-vacuity for the test above: the method must be one httpcore actually calls.

    Overriding a method the library no longer uses passes the test above forever while guarding
    nothing — the same failure mode `test_guarded_client_is_actually_wired` exists to catch for the
    private attributes.
    """
    import httpcore

    assert hasattr(httpcore.NetworkBackend, "connect_unix_socket"), (
        "httpcore no longer defines `connect_unix_socket` — the override guards nothing; find the "
        "method it renamed to and guard that instead"
    )
