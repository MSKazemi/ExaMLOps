"""Interval parsing for source schedules (``15m``, ``6h``, ``1d``, ``@hourly``, ``@daily``)."""

from __future__ import annotations

import re

from examlops.dataplane.types import SpecError

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ALIASES = {"@hourly": 3600, "@daily": 86400, "@weekly": 7 * 86400}


def parse_interval(value: str) -> int:
    v = value.strip().lower()
    if v in _ALIASES:
        return _ALIASES[v]
    m = re.fullmatch(r"(\d+)([smhd])", v)
    if not m or int(m.group(1)) == 0:
        raise SpecError(f"schedule {value!r}: use e.g. 15m, 6h, 1d, @hourly, @daily")
    seconds = int(m.group(1)) * _UNITS[m.group(2)]
    if seconds < 60:
        raise SpecError("schedule must be at least 1m")
    return seconds
