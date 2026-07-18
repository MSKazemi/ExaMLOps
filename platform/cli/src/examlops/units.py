"""Shared HPC-unit + locale formatting (Phase 4 item 4.7 — the i18n formatter half).

The F19 i18n claim needs one canonical formatter for HPC quantities (GPU-hours, bytes, GFLOPS, cost,
carbon, durations) so every surface — CLI, dashboard, reports — renders them identically and
locale-appropriately, instead of each console hand-formatting. This is the dependency-free core
(the frontend imports the same rules); locale controls the decimal/grouping style.
"""

from __future__ import annotations

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
_SI_UNITS = ("", "K", "M", "G", "T", "P")


def _grouped(value: float, *, decimals: int, locale: str) -> str:
    s = f"{value:,.{decimals}f}"
    if locale.startswith(("de", "it", "fr", "es")):
        # European style: '.' groups, ',' decimals.
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return s


def format_bytes(n: float, *, decimals: int = 1, locale: str = "en") -> str:
    """Human bytes with binary (IEC) units: 1536 → '1.5 KiB'."""
    neg = n < 0
    n = abs(float(n))
    i = 0
    while n >= 1024 and i < len(_BYTE_UNITS) - 1:
        n /= 1024.0
        i += 1
    d = 0 if i == 0 else decimals
    return ("-" if neg else "") + f"{_grouped(n, decimals=d, locale=locale)} {_BYTE_UNITS[i]}"


def format_si(n: float, *, unit: str = "", decimals: int = 1, locale: str = "en") -> str:
    """SI-suffixed number: 2_500_000 → '2.5M' (e.g. GFLOPS, request counts)."""
    neg = n < 0
    n = abs(float(n))
    i = 0
    while n >= 1000 and i < len(_SI_UNITS) - 1:
        n /= 1000.0
        i += 1
    d = 0 if i == 0 else decimals
    suffix = _SI_UNITS[i] + unit
    return ("-" if neg else "") + f"{_grouped(n, decimals=d, locale=locale)}{suffix}"


def format_gpu_hours(h: float, *, locale: str = "en") -> str:
    return f"{_grouped(float(h), decimals=1, locale=locale)} GPU-h"


def format_cost(usd: float, *, locale: str = "en") -> str:
    return f"${_grouped(float(usd), decimals=2, locale=locale)}"


def format_carbon(kg: float, *, locale: str = "en") -> str:
    if abs(kg) < 1:
        return f"{_grouped(kg * 1000, decimals=0, locale=locale)} gCO₂e"
    return f"{_grouped(float(kg), decimals=2, locale=locale)} kgCO₂e"


def format_duration(seconds: float) -> str:
    """Compact duration: 3661 → '1h 1m 1s' (locale-independent)."""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)
