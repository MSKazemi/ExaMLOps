"""`examlops.mlflow_paging` — following MLflow's `next_page_token`, written once.

Five call sites had each rediscovered the same bug: read the first page, stop, and hand back a
partial list that nothing can distinguish from a complete one. These tests pin the two properties
that make the shared helper worth having — it reaches the end, and it *refuses* rather than
returning a short list when it cannot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.mlflow_paging import MAX_PAGES, PagingError, all_items  # noqa: E402


def _pager(pages: list[dict], seen: list[str | None] | None = None):
    """A fetch callable serving `pages` in order, keyed by the page_token in the URL."""

    def fetch(url: str) -> dict:
        token = None
        if "page_token=" in url:
            token = url.split("page_token=")[1].split("&")[0]
        if seen is not None:
            seen.append(token)
        return pages[0] if token is None else pages[int(token)]

    return fetch


def test_it_returns_every_item_across_pages():
    pages = [
        {"registered_models": [{"name": "a"}], "next_page_token": "1"},
        {"registered_models": [{"name": "b"}], "next_page_token": "2"},
        {"registered_models": [{"name": "c"}]},
    ]
    seen: list[str | None] = []
    out = all_items(_pager(pages, seen), "http://mlflow/search", "registered_models")
    assert [m["name"] for m in out] == ["a", "b", "c"]
    assert seen == [None, "1", "2"]


def test_it_preserves_a_query_string_the_caller_already_built():
    """`model-versions/search` carries a `filter=`; appending paging must not drop it."""
    urls: list[str] = []

    def fetch(url: str) -> dict:
        urls.append(url)
        return {"model_versions": []}

    all_items(fetch, "http://mlflow/search?filter=name%3D%27jpcp%27", "model_versions")
    assert "filter=name" in urls[0] and "max_results=" in urls[0], urls


def test_a_repeated_token_is_refused_not_truncated():
    """Returning the pages read so far is the original bug wearing a loop.

    The caller cannot tell a short list from a complete one — that is the entire defect — so the
    only honest options are the whole list or an error.
    """
    pages = [{"registered_models": [{"name": "a"}], "next_page_token": "0"}]
    with pytest.raises(PagingError, match="repeated a page token"):
        all_items(lambda url: pages[0], "http://mlflow/search", "registered_models")


def test_a_registry_that_never_stops_paging_is_refused():
    calls = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return {"registered_models": [{"name": "x"}], "next_page_token": str(len(calls))}

    with pytest.raises(PagingError, match="more than"):
        all_items(fetch, "http://mlflow/search", "registered_models")
    assert len(calls) == MAX_PAGES, "it should stop exactly at the cap, not before or after"


def test_a_missing_or_empty_key_is_an_empty_list_not_a_crash():
    """An empty registry is a real state; it must not look like a transport failure."""
    assert all_items(lambda url: {}, "http://mlflow/search", "registered_models") == []
    assert (
        all_items(lambda url: {"registered_models": None}, "http://m/s", "registered_models") == []
    )
