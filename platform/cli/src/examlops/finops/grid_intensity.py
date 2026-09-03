"""Live grid carbon-intensity signal for the ``grid-live`` carbon provider (BL-004).

Fetches the *current* grid carbon intensity (gCO2/kWh) from an operator-configured,
endpoint-agnostic HTTP source (ElectricityMaps / WattTime / a national-grid API), with a short
TTL cache and **graceful degradation**: with no endpoint configured, or on any fetch/parse
error, the caller falls back to the platform's static default — so carbon accounting always
works offline (the project's degrade-to-local DNA). Never raises.

**What kind of signal the endpoint serves is the operator's to declare** (ADR 0112): the
number looks identical whether it is an average grid mix or a locational marginal, and the two
are safe on opposite paths. :func:`current_grid_signal` returns a typed
:class:`~examlops.finops.carbon_signal.CarbonSignal`; :func:`current_grid_intensity` remains the
bare-float accessor for callers that only report.

Config (all optional):
  EXAMLOPS_GRID_INTENSITY_URL     endpoint; may contain a ``{zone}`` placeholder
  EXAMLOPS_GRID_INTENSITY_ZONE    zone/region id (substituted or appended as ?zone=)
  EXAMLOPS_GRID_INTENSITY_TOKEN   optional bearer token for the endpoint
  EXAMLOPS_GRID_INTENSITY_METHOD  what the endpoint measures — default ``average_grid_mix``.
                                  Set to ``locational_marginal`` (or another decision method)
                                  only if the feed really is marginal; the default is the safe
                                  one because a mislabelled decision signal is the harm ADR 0112
                                  exists to prevent.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import UTC, datetime

from .carbon_signal import CarbonSignal

logger = logging.getLogger(__name__)

#: What an unconfigured endpoint is assumed to measure. Average, i.e. reportable and *not*
#: usable for placement — the conservative default, because the opposite mistake is harmful.
DEFAULT_METHOD = "average_grid_mix"
#: The method a static fallback figure has. It is a documented constant, not a measurement.
STATIC_METHOD = "static_default"

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


def configured_method() -> str:
    """The method the operator says the endpoint measures (ADR 0112)."""
    return os.getenv("EXAMLOPS_GRID_INTENSITY_METHOD", "").strip().lower() or DEFAULT_METHOD


def current_grid_signal(default: float) -> CarbonSignal:
    """Current grid carbon intensity **as a typed signal**, or the static default.

    A fallback is never dressed up as a live reading: when the endpoint is unset, unreachable or
    unparseable the returned signal says ``method="static_default"`` and ``source="fallback"``,
    so a caller reporting the figure states what it actually is. Never raises — a carbon signal
    that takes the platform down is worse than one that degrades and says so.
    """
    zone = os.getenv("EXAMLOPS_GRID_INTENSITY_ZONE", "").strip()
    now_iso = datetime.now(UTC).isoformat()
    url = os.getenv("EXAMLOPS_GRID_INTENSITY_URL", "").strip()

    def _fallback() -> CarbonSignal:
        return CarbonSignal(
            grams_per_kwh=default,
            method=STATIC_METHOD,
            zone=zone,
            source="fallback",
            fetched_at=now_iso,
        )

    if not url:
        return _fallback()
    full = _build_url(url, zone)
    now = time.monotonic()
    cached = _cache.get(full)
    if cached is not None and cached[1] > now:
        val: float | None = cached[0]
    else:
        val = _fetch(full)
        if val is not None and val > 0:
            _cache[full] = (val, now + _CACHE_TTL_S)
    if val is None or val <= 0:
        return _fallback()
    return CarbonSignal(
        grams_per_kwh=val,
        method=configured_method(),
        zone=zone,
        source=full,
        fetched_at=now_iso,
    )


def current_grid_intensity(default: float) -> float:
    """Current grid carbon intensity (gCO2/kWh), or ``default`` when unavailable.

    The bare-float accessor, unchanged for every existing caller. Anything that *decides*
    something with the number must use :func:`current_grid_signal` instead and let ADR 0112's
    guards check it — a float carries no record of what it measures, which is precisely how an
    average signal ends up driving a placement.
    """
    return current_grid_signal(default).grams_per_kwh
