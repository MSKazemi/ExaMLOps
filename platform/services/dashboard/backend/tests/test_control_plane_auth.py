"""The dashboard authenticates to the control plane (plan P0.3 / finding B3).

The control plane put ``read``-scope auth on ``/models*`` on 2026-09-04. This client sent no
credential, and its docstring said the routes were public, so the Models pages and every bundled
README image broke the moment the server was hardened. These tests pin the client half of that
contract; ``tests/unit/test_control_plane_consumers.py`` pins it across every consumer.
"""

from __future__ import annotations

import time

import control_plane_auth
import httpx
import pytest
from control_plane_client import ControlPlaneClient
from settings import settings


def _client(handler, token: str | None) -> ControlPlaneClient:
    async def _provider() -> str | None:
        return token

    return ControlPlaneClient(
        base_url="http://cp", transport=httpx.MockTransport(handler), token_provider=_provider
    )


async def test_reads_send_the_bearer_credential():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[{"model_name": "JPCP", "datasets": []}])

    assert await _client(handler, "cp-read-token").list_model_names() == ["JPCP"]
    assert seen == ["Bearer cp-read-token"]


async def test_no_credential_configured_sends_no_header():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    await _client(handler, None).list_model_names()
    assert seen == [None]


async def test_bundled_image_is_fetched_server_side_with_the_credential():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models/JPCP/images/arch.png"
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    data, content_type = await _client(handler, "t").get_bundled_image("JPCP", "arch.png")
    assert (data, content_type) == (b"\x89PNG", "image/png")


async def test_missing_bundled_image_is_a_key_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(KeyError):
        await _client(handler, "t").get_bundled_image("JPCP", "nope.png")


# ─── signed image URLs ────────────────────────────────────────────────────────


def _parts(path: str) -> tuple[str, str, int, str]:
    prefix, query = path.split("?")
    _, _, _, name, _, filename = prefix.split("/")
    params = dict(p.split("=") for p in query.split("&"))
    return name, filename, int(params["exp"]), params["sig"]


def test_signed_image_path_round_trips():
    name, filename, exp, sig = _parts(control_plane_auth.signed_image_path("JPCP", "arch.png"))
    assert (name, filename) == ("JPCP", "arch.png")
    assert control_plane_auth.verify_image_signature(name, filename, exp, sig)


def test_expired_signature_is_refused():
    past = time.time() - 2 * control_plane_auth.IMAGE_URL_TTL_SECONDS
    path = control_plane_auth.signed_image_path("JPCP", "arch.png", now=past)
    assert not control_plane_auth.verify_image_signature(*_parts(path))


def test_signature_does_not_transfer_to_another_image():
    name, _filename, exp, sig = _parts(control_plane_auth.signed_image_path("JPCP", "arch.png"))
    assert not control_plane_auth.verify_image_signature(name, "secret.png", exp, sig)
    assert not control_plane_auth.verify_image_signature("MACK", "arch.png", exp, sig)


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", ".hidden", "", "x" * 200])
def test_unsafe_segments_are_never_signed_or_accepted(bad):
    with pytest.raises(ValueError):
        control_plane_auth.signed_image_path("JPCP", bad)
    assert not control_plane_auth.verify_image_signature("JPCP", bad, int(time.time()) + 60, "0")


# ─── credential precedence ────────────────────────────────────────────────────


async def test_store_secret_wins_over_the_environment(monkeypatch):
    async def _store() -> str | None:
        return "from-config-page"

    monkeypatch.setattr(control_plane_auth, "_read_store_secret", _store)
    monkeypatch.setattr(settings, "control_plane_token", "from-env")
    control_plane_auth.reset_token_cache()
    assert await control_plane_auth.control_plane_token() == "from-config-page"


async def test_environment_is_the_fallback_when_the_store_is_unset(monkeypatch):
    async def _store() -> str | None:
        return None

    monkeypatch.setattr(control_plane_auth, "_read_store_secret", _store)
    monkeypatch.setattr(settings, "control_plane_token", "from-env")
    control_plane_auth.reset_token_cache()
    assert await control_plane_auth.control_plane_token() == "from-env"


async def test_unreadable_store_falls_back_instead_of_failing(monkeypatch):
    async def _store() -> str | None:
        raise RuntimeError("store down")

    monkeypatch.setattr(control_plane_auth, "_read_store_secret", _store)
    monkeypatch.setattr(settings, "control_plane_token", "")
    control_plane_auth.reset_token_cache()
    assert await control_plane_auth.control_plane_token() is None


# ─── the image route, end to end through the app ──────────────────────────────


@pytest.fixture
def image_cp(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/models/JPCP/images/arch.png":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        if request.url.path == "/models/JPCP/images/page.html":
            return httpx.Response(200, content=b"<script>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    monkeypatch.setattr("routers.models._control_plane", lambda: _client(handler, "t"))


@pytest.mark.asyncio
async def test_signed_link_serves_the_image_without_a_session(client, image_cp):
    path = control_plane_auth.signed_image_path("JPCP", "arch.png")
    response = await client.get(path)
    assert response.status_code == 200
    assert response.content == b"\x89PNG"
    assert response.headers["cache-control"].startswith("private")


@pytest.mark.asyncio
async def test_tampered_link_is_403(client, image_cp):
    path = control_plane_auth.signed_image_path("JPCP", "arch.png")
    response = await client.get(path.replace("sig=", "sig=0"))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_non_image_upstream_content_is_never_served(client, image_cp):
    response = await client.get(control_plane_auth.signed_image_path("JPCP", "page.html"))
    assert response.status_code == 502
