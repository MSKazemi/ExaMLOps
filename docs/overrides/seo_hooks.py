"""MkDocs hook: give every docs page its own meta description.

A page without ``description:`` front matter inherits ``site_description``, so 117 of 138 pages
once shared one summary — the text a search result or an AI answer shows for the page. This hook
derives the description from the page's own first real paragraph instead: accurate by
construction, nothing to keep in sync. Explicit front matter always wins.

Registered in mkdocs.yml under ``hooks:``; the theme reads ``page.meta.description`` for the
``<meta name="description">`` tag, the OpenGraph/Twitter tags and the JSON-LD in main.html.
"""

from __future__ import annotations

import html
import re
from typing import Any

MAX_LEN = 160
MIN_LEN = 40  # shorter paragraphs are badges, labels or stubs, not a summary

_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.S)  # bare <p> only: skips admonition titles
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def describe(page_html: str, max_len: int = MAX_LEN) -> str | None:
    """Return the first paragraph of ``page_html`` as plain text of at most ``max_len`` chars."""
    for match in _PARAGRAPH.finditer(page_html):
        text = _SPACE.sub(" ", html.unescape(_TAG.sub("", match.group(1)))).strip()
        if len(text) < MIN_LEN:
            continue
        if len(text) <= max_len:
            return text
        cut = text[: max_len - 1].rsplit(" ", 1)[0].rstrip(",;:—-– ")
        return cut + "…"
    return None


def on_page_content(html_: str, page: Any, config: Any, files: Any) -> str:
    meta = page.meta if page.meta is not None else {}
    if not meta.get("description"):
        description = describe(html_)
        if description:
            meta["description"] = description
            page.meta = meta
    return html_
