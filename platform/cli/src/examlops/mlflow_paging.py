"""Follow MLflow's ``next_page_token`` — written once, for every caller that lists a registry.

MLflow's ``*/search`` endpoints return a bounded page plus a ``next_page_token``, whether or not
the caller asked for a page size. A caller that reads the page and stops gets a partial list that
is **indistinguishable from a complete one**: nothing failed, nothing was caught, and the tests
pass — the fakes hand back everything at once.

That would merely be a short list if the result were only displayed. It is usually not: callers
filter it (models carrying a Production alias), match a name against it, or count it. Then a
partial read does not shorten the answer, it changes it — *no model is in production*, *no models
found*, *100 registered models* forever.

Two rules are baked in here rather than left to each caller:

* **Follow the token to the end.** ``examlops.serving_snapshot._registered_models`` has done this
  since the platform's own "100-model bug", and every caller that did not was a rediscovery of it.
* **Refuse rather than truncate.** A registry that repeats a token or never stops paging raises
  :class:`PagingError`. Returning what was read so far is the original bug wearing a loop, and the
  caller cannot tell the difference — which is the whole defect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

#: Asked for explicitly so the page count stays small on a large registry. It is not a limit on
#: what this function returns — that is the point of the loop.
DEFAULT_PAGE_SIZE = 1000

#: Pages followed before giving up. At the default page size this is far past any real registry;
#: it exists so a misbehaving server cannot pin a caller forever.
MAX_PAGES = 100


class PagingError(RuntimeError):
    """A paginated read could not be completed, so no answer is better than a partial one."""


def _with_params(url: str, params: dict[str, str]) -> str:
    """``url`` with ``params`` merged into its query string, preserving what was already there."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))


def all_items(
    fetch: Callable[[str], dict[str, Any]],
    url: str,
    key: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Every item under ``key``, following ``next_page_token`` until it is gone.

    ``fetch`` is the caller's own transport — it takes a URL and returns the parsed body — so this
    works for ``urllib``, ``httpx`` and the agent's request wrapper without any of them needing to
    agree on anything else.

    Raises :class:`PagingError` if the server repeats a page token or keeps paging past
    ``max_pages``.
    """
    items: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        params = {"max_results": str(page_size)}
        if token:
            params["page_token"] = token
        body = fetch(_with_params(url, params)) or {}
        items.extend(body.get(key) or [])
        token = body.get("next_page_token")
        if not token:
            return items
        if token in seen:
            raise PagingError(f"MLflow repeated a page token for {url!r}; the list is incomplete")
        seen.add(token)
    raise PagingError(f"MLflow returned more than {max_pages} pages for {url!r}")
