"""Async httpx wrapper around the control-plane read endpoints.

The dashboard only needs read traffic — control-plane authenticates writes
with its own token, but /models/* and /models/{name}/* are public reads.
"""
from __future__ import annotations

import httpx


class ControlPlaneClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def list_model_names(self) -> list[str]:
        async with self._client() as c:
            r = await c.get("/models")
            r.raise_for_status()
            return [row["model_name"] for row in r.json()]

    async def get_meta(self, name: str) -> dict:
        async with self._client() as c:
            r = await c.get(f"/models/{name}/meta")
            if r.status_code == 404:
                raise KeyError(name)
            r.raise_for_status()
            return r.json()

    async def get_readme(self, name: str) -> tuple[str, str]:
        async with self._client() as c:
            r = await c.get(f"/models/{name}/readme")
            r.raise_for_status()
            payload = r.json()
            return payload.get("text", ""), payload.get("sha", "")

    def bundled_image_url(self, name: str, filename: str) -> str:
        """URL the browser can fetch directly. No async; pure string."""
        return f"{self._base_url}/models/{name}/images/{filename}"
