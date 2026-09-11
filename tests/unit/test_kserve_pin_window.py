# tests/unit/test_kserve_pin_window.py
"""USAR I0 — the KServe pin cannot age out silently (spec-usar-1 R-SUB-22, ADR 0142 d4).

KServe releases a minor every 8 weeks and supports only N and N-1. A vendored schema that falls
out of that window validates renders against an API nobody patches any more. This guard fails the
build once the pin is older than its support window, with the instruction to re-pin — on purpose:
the failure is the reminder.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.serving.substrates import k8s_schema  # noqa: E402


def test_the_pinned_kserve_release_is_inside_its_support_window():
    pin = k8s_schema.current_pin()
    message = k8s_schema.pin_window_error(pin, date.today())
    assert message is None, message


def test_the_window_is_two_eight_week_cycles_from_the_release():
    pin = k8s_schema.current_pin()
    assert pin.window_weeks == 16
    assert pin.expires == date(2026, 8, 6) + timedelta(weeks=16) == date(2026, 11, 26)


def test_the_guard_can_fail():
    """A pin past its window produces the re-pin instruction (the opposite arm)."""
    stale = k8s_schema.Pin("v0.1.0", date(2020, 1, 1), 16)
    message = k8s_schema.pin_window_error(stale, date(2026, 9, 11))
    assert message and "re-" in message and "v0.1.0" in message
    assert k8s_schema.pin_window_error(stale, stale.expires) is None  # the last day still passes
