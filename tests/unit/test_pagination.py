"""Keyset pagination primitive (enterprise-readiness Phase 4, item 4.3).

Proves stable, constant-cost cursor pagination: pages chain via opaque cursors, the last page has
no next cursor, page size is capped, the SQL seek clause is correct, and walking the cursors
reconstructs the full set with no gaps or dupes even as the tail is fetched incrementally.
"""

from __future__ import annotations

from examlops.pagination import (
    MAX_PAGE_SIZE,
    clamp_page_size,
    decode_cursor,
    encode_cursor,
    fetch_limit,
    paginate,
    seek_clause,
)


def test_clamp_page_size():
    assert clamp_page_size(None) == 50
    assert clamp_page_size(0) == 50
    assert clamp_page_size(10) == 10
    assert clamp_page_size(10_000) == MAX_PAGE_SIZE


def test_cursor_roundtrip():
    c = encode_cursor(123)
    assert decode_cursor(c) == 123
    assert decode_cursor(None) is None
    assert decode_cursor("not-base64!!") is None  # malformed → first page


def test_paginate_detects_next_page():
    rows = [{"id": i} for i in range(6)]  # fetched page_size(5)+1
    page = paginate(rows, page_size=5)
    assert len(page["items"]) == 5
    assert page["has_more"] is True
    assert decode_cursor(page["next_cursor"]) == page["items"][-1]["id"]


def test_paginate_last_page_has_no_cursor():
    rows = [{"id": i} for i in range(3)]
    page = paginate(rows, page_size=5)
    assert page["has_more"] is False and page["next_cursor"] is None


def test_seek_clause():
    assert seek_clause(None) == ("", [])
    frag, params = seek_clause(encode_cursor(42))
    assert frag == "id < ?" and params == [42]
    frag2, _ = seek_clause(encode_cursor(42), descending=False)
    assert frag2 == "id > ?"


def test_fetch_limit_is_page_size_plus_one():
    assert fetch_limit(5) == 6
    assert fetch_limit(None) == 51


def test_walking_cursors_covers_all_rows_no_dupes():
    # Simulate a table of 23 rows, descending by id, paged 10 at a time via the seek clause.
    table = [{"id": i, "v": f"row{i}"} for i in range(23)]

    def query(cursor, page_size):
        frag, params = seek_clause(cursor)
        rows = sorted(table, key=lambda r: r["id"], reverse=True)
        if frag:
            rows = [r for r in rows if r["id"] < params[0]]
        return rows[: fetch_limit(page_size)]

    seen: list[int] = []
    cursor = None
    for _ in range(10):  # safety bound
        page = paginate(query(cursor, 10), page_size=10)
        seen.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break

    assert sorted(seen) == list(range(23))  # every row exactly once
    assert len(seen) == len(set(seen))  # no duplicates
