"""Regression guard: the API docs must render under the strict CSP (F16 / ADR 0053).

FastAPI's default ``/docs`` + ``/redoc`` load Swagger UI / ReDoc from ``cdn.jsdelivr.net``.
The SecurityHeadersMiddleware sets ``script-src 'self'``, so the browser refuses those CDN
scripts and the docs page renders blank. main.py re-serves both pages from same-origin
vendored assets under ``/static``; these tests fail if anyone reintroduces a CDN reference
or drops a vendored asset.
"""

import pytest


@pytest.mark.asyncio
async def test_swagger_docs_are_same_origin_no_cdn(client):
    resp = await client.get("/docs")
    assert resp.status_code == 200
    body = resp.text
    # No public CDN references — the strict CSP would block them in the browser.
    assert "cdn.jsdelivr.net" not in body
    assert "fastapi.tiangolo.com" not in body
    # Assets are served same-origin under /static.
    assert "/static/swagger-ui-bundle.js" in body
    assert "/static/swagger-ui.css" in body


@pytest.mark.asyncio
async def test_redoc_is_same_origin_no_cdn(client):
    resp = await client.get("/redoc")
    assert resp.status_code == 200
    body = resp.text
    assert "cdn.jsdelivr.net" not in body
    assert "fonts.googleapis.com" not in body  # Google Fonts stylesheet is CSP-blocked too.
    assert "/static/redoc.standalone.js" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset",
    [
        "/static/swagger-ui-bundle.js",
        "/static/swagger-ui.css",
        "/static/redoc.standalone.js",
        "/static/favicon.png",
    ],
)
async def test_vendored_docs_assets_are_served(client, asset):
    resp = await client.get(asset)
    assert resp.status_code == 200, f"{asset} not served — vendored asset missing?"
    assert int(resp.headers.get("content-length", "1")) > 0


@pytest.mark.asyncio
async def test_openapi_schema_has_paths(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert len(resp.json().get("paths", {})) > 0
