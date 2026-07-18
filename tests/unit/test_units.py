"""Shared HPC-unit formatter (enterprise-readiness Phase 4, item 4.7).

Proves one canonical formatter for GPU-hours / bytes / SI / cost / carbon / duration, with
locale-aware decimal+grouping style, so every surface renders quantities identically.
"""

from __future__ import annotations

from examlops.units import (
    format_bytes,
    format_carbon,
    format_cost,
    format_duration,
    format_gpu_hours,
    format_si,
)


def test_format_bytes_iec():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KiB"
    assert format_bytes(1024**3) == "1.0 GiB"
    assert format_bytes(-2048) == "-2.0 KiB"


def test_format_si():
    assert format_si(2_500_000, unit="FLOPS") == "2.5MFLOPS"
    assert format_si(999) == "999"
    assert format_si(1000) == "1.0K"


def test_format_gpu_hours_and_cost():
    assert format_gpu_hours(12.5) == "12.5 GPU-h"
    assert format_cost(1234.5) == "$1,234.50"


def test_format_carbon_scales():
    assert format_carbon(0.4) == "400 gCO₂e"  # < 1 kg → grams
    assert format_carbon(2.5) == "2.50 kgCO₂e"


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(61) == "1m 1s"
    assert format_duration(3661) == "1h 1m 1s"
    assert format_duration(90061) == "1d 1h 1m 1s"


def test_locale_grouping_and_decimal():
    # European locale flips grouping '.' and decimal ','.
    assert format_cost(1234.5, locale="de") == "$1.234,50"
    assert format_gpu_hours(1000.5, locale="it") == "1.000,5 GPU-h"
    # English default stays comma-grouped, dot-decimal.
    assert format_cost(1234.5, locale="en") == "$1,234.50"
