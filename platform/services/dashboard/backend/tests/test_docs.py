"""Docs router: viewer-gated tree + content; path-traversal rejected."""

import pytest

from tests.conftest import VIEWER_PW


async def _login(client, pw):
    r = await client.post("/api/auth/login", json={"password": pw})
    return r.json()["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_docs_tree_returns_sections(client):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/docs/tree", headers=_hdr(token))
    assert response.status_code == 200
    sections = response.json()
    assert isinstance(sections, list)
    assert len(sections) > 0
    for s in sections:
        assert "key" in s
        assert "title" in s
        assert "files" in s
        assert isinstance(s["files"], list)
        assert len(s["files"]) > 0


@pytest.mark.asyncio
async def test_docs_tree_requires_auth(client):
    response = await client.get("/api/docs/tree")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_docs_tree_files_have_path_and_title(client):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/docs/tree", headers=_hdr(token))
    sections = response.json()
    for section in sections:
        for f in section["files"]:
            assert "path" in f
            assert "title" in f
            assert f["path"].endswith(".md")


@pytest.mark.asyncio
async def test_docs_content_returns_markdown(client):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/docs/content?path=README.md", headers=_hdr(token))
    assert response.status_code == 200
    assert "ExaMLOps" in response.text or len(response.text) > 0


@pytest.mark.asyncio
async def test_docs_content_requires_auth(client):
    response = await client.get("/api/docs/content?path=README.md")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_docs_content_missing_file_returns_404(client):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/docs/content?path=docs/nonexistent.md", headers=_hdr(token))
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_docs_content_rejects_path_traversal(client):
    token = await _login(client, VIEWER_PW)
    response = await client.get("/api/docs/content?path=../../etc/passwd", headers=_hdr(token))
    assert response.status_code in (400, 404)
