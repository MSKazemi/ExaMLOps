"""Live grid carbon-intensity signal for the ``grid-live`` carbon provider (BL-004).

Fetches the *current* grid carbon intensity (gCO2/kWh) from an operator-configured,
endpoint-agnostic HTTP source (ElectricityMaps / WattTime / a national-grid API), with a short
TTL cache and **graceful degradation**: with no endpoint configured, or on any fetch/parse
error, the caller falls back to the platform's static default — so carbon accounting always
works offline (the project's degrade-to-local DNA). Never raises.

Config (all optional):
  EXAMLOPS_GRID_INTENSITY_URL     endpoint; may contain a ``{zone}`` placeholder
  EXAMLOPS_GRID_INTENSITY_ZONE    zone/region id (substituted or appended as ?zone=)
  EXAMLOPS_GRID_INTENSITY_TOKEN   optional bearer token for the endpoint
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 300.0  # 5 min — grid intensity changes slowly; avoids hammering the endpoint.
_cache: dict[str, tuple[float, float]] = {}  # url -> (value, expires_at_monotonic)

# Common keys a grid-intensity endpoint uses for the gCO2/kWh figure.
_INTENSITY_KEYS = (
    "carbonIntensity",
    "carbon_intensity",
    "gco2_per_kwh",
    "intensity",
    "value",
)


def _extract_intensity(data: object) -> float | None:
    """Pull a numeric gCO2/kWh from a JSON body, trying common keys (nested one level deep)."""
    if isinstance(data, bool):  # guard: bool is an int subclass
        return None
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        for k in _INTENSITY_KEYS:
            v = data.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        for v in data.values():  # e.g. {"data": {"carbonIntensity": 123}}
            if isinstance(v, dict):
                got = _extract_intensity(v)
                if got is not None:
                    return got
    return None


def _build_url(url: str, zone: str) -> str:
    if "{zone}" in url:
        return url.replace("{zone}", zone)
    if zone:
        return f"{url}{'&' if '?' in url else '?'}zone={zone}"
    return url


def _fetch(url: str, timeout: float = 5.0) -> float | None:
    """GET the endpoint and extract the intensity. Returns None on any error (fail-open)."""
    headers = {}
    token = os.getenv("EXAMLOPS_GRID_INTENSITY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - operator-configured URL
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode())
        return _extract_intensity(body)
    except Exception as exc:  # pragma: no cover - network path, exercised via monkeypatched _fetch
        logger.warning("grid-intensity fetch failed (%s); using fallback", exc)
        return None


def clear_cache() -> None:
    """Drop the TTL cache (used by tests / after a zone change)."""
    _cache.clear()


def current_grid_intensity(default: float) -> float:
    """Current grid carbon intensity (gCO2/kWh), or ``default`` when unavailable.

    Unset ``EXAMLOPS_GRID_INTENSITY_URL`` ⇒ ``default`` (degrade). A non-positive or unparseable
    reading also degrades to ``default``. Cached for ``_CACHE_TTL_S``. Never raises.
    """
    url = os.getenv("EXAMLOPS_GRID_INTENSITY_URL", "").strip()
    if not url:
        return default
    full = _build_url(url, os.getenv("EXAMLOPS_GRID_INTENSITY_ZONE", "").strip())
    now = time.monotonic()
    cached = _cache.get(full)
    if cached is not None and cached[1] > now:
        return cached[0]
    val = _fetch(full)
    if val is None or val <= 0:
        return default
    _cache[full] = (val, now + _CACHE_TTL_S)
    return val
