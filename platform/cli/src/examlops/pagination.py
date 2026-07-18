"""Keyset (cursor) pagination for fleet-scale list endpoints (Phase 4 item 4.3).

Offset pagination (`LIMIT n OFFSET m`) degrades linearly — page 10 000 rescans a million rows — and
skips/duplicates rows when the set changes underneath it. Keyset pagination seeks on an ordered,
unique key (`WHERE id < :cursor ORDER BY id DESC LIMIT :n+1`) so every page costs the same and is
stable under concurrent writes. This module is the backend primitive the DataGrid / fleet / queue /
registry views page with; a hard `max_page_size` caps blast radius, and the opaque cursor keeps the
key out of the client's business.

Pure functions over row lists → fully offline-testable; a SQL helper builds the seek clause.
"""

from __future__ import annotations

import base64
import json
from typing import Any

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 50


def clamp_page_size(size: int | None) -> int:
    """Clamp a requested page size into ``[1, MAX_PAGE_SIZE]`` (default when None/invalid)."""
    if not size or size < 1:
        return DEFAULT_PAGE_SIZE
    return min(int(size), MAX_PAGE_SIZE)


def encode_cursor(key_value: Any) -> str:
    """Opaque, URL-safe cursor for a key value (so clients treat it as a token, not an id)."""
    raw = json.dumps({"k": key_value}, default=str).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str | None) -> Any:
    """Decode a cursor back to its key value, or None if absent/malformed (→ first page)."""
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["k"]
    except Exception:  # noqa: BLE001 - a bad cursor just starts from the top
        return None


def paginate(
    rows: list[dict[str, Any]], *, key: str = "id", page_size: int | None = None
) -> dict[str, Any]:
    """Slice one keyset page from ``rows`` (already ordered by ``key`` descending).

    ``rows`` should be fetched with ``page_size + 1`` rows so we can detect a next page without a
    count. Returns ``{"items", "next_cursor", "has_more", "page_size"}``. ``next_cursor`` is None on
    the last page.
    """
    size = clamp_page_size(page_size)
    has_more = len(rows) > size
    items = rows[:size]
    next_cursor = encode_cursor(items[-1][key]) if has_more and items else None
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "page_size": size,
    }


def seek_clause(
    cursor: str | None, *, key: str = "id", descending: bool = True
) -> tuple[str, list[Any]]:
    """Build the SQL seek predicate + params for a cursor.

    Returns ``(where_fragment, params)`` — e.g. ``("id < ?", [123])`` — or ``("", [])`` for the first
    page. Compose into a query as ``WHERE <seek> ORDER BY <key> DESC LIMIT <page_size + 1>``. ``key``
    is caller-supplied (a column name, never user input) so string interpolation is safe.
    """
    value = decode_cursor(cursor)
    if value is None:
        return "", []
    op = "<" if descending else ">"
    return f"{key} {op} ?", [value]


def fetch_limit(page_size: int | None) -> int:
    """The row count to SELECT (page_size + 1) so :func:`paginate` can detect a next page."""
    return clamp_page_size(page_size) + 1
