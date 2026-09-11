"""ADR 0130 — rest connector: auth, pagination variants, incremental."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.dataplane.connectors import rest  # noqa: E402
from examlops.dataplane.types import EgressDenied, Limits, SpecError  # noqa: E402

ITEMS = [{"id": i, "v": i * 2} for i in range(1, 8)]
CONN = {
    "kind": "rest",
    "base_url": "https://api.example.org",
    "auth": "bearer",
    "secret": "tok-123456",
}


@pytest.fixture
def seen(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        q = request.url.params
        items = [i for i in ITEMS if i["id"] > int(q.get("since", 0))]
        if "page" in q:
            size, page = int(q["per_page"]), int(q["page"])
            return httpx.Response(
                200, json={"data": {"items": items[(page - 1) * size : page * size]}}
            )
        if "cursor" in q or request.url.path.endswith("/cursor"):
            start = int(q.get("cursor") or 0)
            chunk = items[start : start + 3]
            nxt = start + 3 if start + 3 < len(items) else None
            return httpx.Response(200, json={"items": chunk, "next": nxt})
        if request.url.path.endswith("/offset"):
            limit = int(q.get("limit", 3))
            off = int(q.get("offset", 0))
            return httpx.Response(200, json=items[off : off + limit])
        if request.url.path.endswith("/link-abs"):
            start = int(q.get("start", 0))
            chunk = items[start : start + 3]
            nxt = start + 3
            headers = {}
            if nxt < len(items):
                headers["Link"] = f'<https://api.example.org/link-abs?start={nxt}>; rel="next"'
            return httpx.Response(200, json=chunk, headers=headers)
        if request.url.path.endswith("/link-evil"):
            return httpx.Response(
                200, json=items[:3], headers={"Link": '<https://evil.example.org/next>; rel="next"'}
            )
        if request.url.path.endswith("/link"):
            start = int(q.get("start", 0))
            chunk = items[start : start + 3]
            nxt = start + 3
            headers = {}
            if nxt < len(items):
                headers["Link"] = f'</link?start={nxt}>; rel="next"'
            return httpx.Response(200, json=chunk, headers=headers)
        if request.url.path.endswith("/mixed"):
            return httpx.Response(200, json=[{"id": 1}, {"id": "x"}])
        if request.url.path.endswith("/badjson"):
            return httpx.Response(
                200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, json=items)

    monkeypatch.setattr(
        rest,
        "_client_factory",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


def _ids(spec, since=None):
    return [
        r["id"]
        for tb in rest.RestConnector().read(CONN, spec, since, Limits())
        for r in tb.batch.to_pylist()
    ]


def test_bearer_auth_header_is_sent(seen):
    _ids({"path": "/items"})
    assert seen[0].headers["authorization"] == "Bearer tok-123456"


def test_page_pagination_stops_on_empty_page(seen):
    spec = {
        "path": "/items",
        "records_path": "data.items",
        "pagination": {"type": "page", "param": "page", "size_param": "per_page", "size": 3},
    }
    assert _ids(spec) == list(range(1, 8))


def test_cursor_pagination(seen):
    spec = {
        "path": "/cursor",
        "records_path": "items",
        "pagination": {"type": "cursor", "param": "cursor", "cursor_path": "next"},
    }
    assert _ids(spec) == list(range(1, 8))


def test_incremental_sends_since(seen):
    spec = {"path": "/items", "watermark_field": "id", "since_param": "since", "incremental": True}
    assert _ids(spec, {"field": "id", "value": 5}) == [6, 7]


def test_offset_pagination_advances_by_returned_count(seen):
    spec = {
        "path": "/offset",
        "pagination": {"type": "offset", "param": "offset", "size_param": "limit", "size": 3},
    }
    assert _ids(spec) == list(range(1, 8))


def test_link_pagination_relative_next_walks_all_pages(seen):
    spec = {"path": "/link", "pagination": {"type": "link"}}
    assert _ids(spec) == list(range(1, 8))


def test_link_pagination_absolute_next_walks_all_pages(seen):
    spec = {"path": "/link-abs", "pagination": {"type": "link"}}
    assert _ids(spec) == list(range(1, 8))


def test_link_pagination_cross_origin_is_denied(seen):
    spec = {"path": "/link-evil", "pagination": {"type": "link"}}
    with pytest.raises(EgressDenied):
        _ids(spec)
    assert seen  # the first (same-origin) page was requested
    assert all(r.url.host != "evil.example.org" for r in seen)


def test_watermark_mixed_types_raises_specerror(seen):
    with pytest.raises(SpecError, match="mixed types"):
        _ids({"path": "/mixed", "watermark_field": "id"})


def test_non_json_response_raises_specerror(seen):
    with pytest.raises(SpecError, match="non-JSON"):
        _ids({"path": "/badjson"})
