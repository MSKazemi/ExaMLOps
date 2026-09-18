# tests/unit/test_slo_status_window.py
"""ADR 0023 — an SLO's status covers the window the SLO declares (BL-075).

Found auditing an **Accepted** ADR against the code. The guide's own definition is "a target for an
SLI over a window (e.g. 99% over 30 days)", specs are written with `--window 30d`, and the module
says as much beside its drift constant: "an SLO is a statement about a rolling window, and the
window's own length lives on the spec".

`slo_status` did not read it. It summed the newest 1000 samples whatever their age — a *count*, not
a window: an hour on a busy SLO, half a year on a quiet one, and on any SLO old enough, samples
from long before the window still counted. Everything downstream inherited it: the error budget,
the burn rate, the `slo_breached` transition audit, and `exa pipeline promote`, which refuses a
release when a gate-flagged budget is exhausted.

The row cap remains, as a bound on how much is read rather than as the definition.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import slo  # noqa: E402
from examlops.platform_db import get_db, init_db, upsert_slo_spec  # noqa: E402

MODEL = "JPCP"


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _spec(window: str = "30d", target: float = 0.99, name: str = "latency"):
    upsert_slo_spec(MODEL, name, sli_source="c1", target=target, sli_query="errors", window=window)


def _sample(good: float, total: float, *, age: timedelta, name: str = "latency"):
    """One recorded sample, aged into the past."""
    when = (datetime.now(UTC) - age).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO slo_samples (model, tenant, name, good, total, ts) VALUES (?,?,?,?,?,?)",
            (MODEL, "default", name, good, total, when),
        )


def _status(name: str = "latency"):
    return slo.slo_status(MODEL, name)[0]


# ── the window decides what counts ───────────────────────────────────────────


def test_a_sample_older_than_the_window_does_not_count():
    _spec("30d")
    _sample(0, 1000, age=timedelta(days=45))  # a bad month, long over
    _sample(100, 100, age=timedelta(days=1))

    status = _status()

    assert (status.n, status.sli) == (100, 1.0)
    assert status.ok is True, "an SLO recovered a month ago is not still breached"


def test_a_sample_inside_the_window_counts():
    _spec("30d")
    _sample(90, 100, age=timedelta(days=29))

    status = _status()

    assert status.n == 100 and status.sli == pytest.approx(0.9)


def test_a_short_window_excludes_what_a_long_one_includes():
    _spec("24h")
    _sample(0, 100, age=timedelta(days=3))
    _sample(100, 100, age=timedelta(hours=2))

    assert _status().sli == 1.0

    _spec("7d")  # same SLO, wider window
    assert _status().sli == pytest.approx(0.5)


def test_the_status_says_which_window_it_measured():
    _spec("7d")

    status = _status()

    assert status.window == "7d"
    assert status.window_start is not None
    assert status.as_dict()["window"] == "7d"


def test_a_window_nothing_can_parse_falls_back_to_the_default_and_says_so():
    _spec("last month")
    _sample(50, 100, age=timedelta(days=2))

    status = _status()

    assert status.window == slo.DEFAULT_WINDOW
    assert status.n == 100, "still measured — a malformed window is not a reason to report nothing"


def test_a_spec_with_no_window_uses_the_default():
    upsert_slo_spec(MODEL, "unwindowed", sli_source="c1", target=0.9, sli_query="errors")
    _sample(1, 1, age=timedelta(days=1), name="unwindowed")

    assert slo.slo_status(MODEL, "unwindowed")[0].window == slo.DEFAULT_WINDOW


# ── what the window changes downstream ───────────────────────────────────────


def test_the_budget_and_burn_rate_follow_the_window():
    _spec("24h", target=0.99)
    _sample(0, 100, age=timedelta(days=10))  # 100 % errors, outside the window
    _sample(99, 100, age=timedelta(hours=1))  # 1 % errors, inside it

    status = _status()

    assert status.sli == pytest.approx(0.99)
    assert status.burn_rate == pytest.approx(1.0), "1% observed against a 1% budget"
    assert status.budget_remaining == pytest.approx(0.0)


def test_an_old_breach_no_longer_blocks_a_promotion():
    """`exa pipeline promote` refuses on an exhausted budget; stale evidence must not do that."""
    _spec("24h", target=0.99)
    _sample(0, 1000, age=timedelta(days=5))

    assert slo.budget_exhausted(MODEL, "latency") is False


def test_a_breach_inside_the_window_still_blocks():
    _spec("24h", target=0.99)
    _sample(0, 1000, age=timedelta(hours=1))

    assert slo.budget_exhausted(MODEL, "latency") is True


def test_an_slo_with_no_samples_in_its_window_is_unmeasured():
    """Not "perfect" — the distinction the status already draws, now also across time."""
    _spec("24h")
    _sample(100, 100, age=timedelta(days=30))

    status = _status()

    assert status.measured is False and status.ok is None and status.n == 0


def test_the_status_table_shows_the_window():
    """An operator reading a row has to know what period its numbers cover."""
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _spec("7d")
    _sample(99, 100, age=timedelta(days=1))

    result = CliRunner().invoke(app, ["slo", "status", MODEL])

    assert result.exit_code == 0, result.output
    assert "Window" in result.output and "7d" in result.output


# ── the row cap is a bound, not the window ───────────────────────────────────


def test_the_cap_bounds_how_much_is_read():
    from examlops.data.governance import slo_sli_ratio

    _spec("30d")
    for _ in range(5):
        _sample(1, 1, age=timedelta(hours=1))

    good, total = slo_sli_ratio(MODEL, "latency", last_n=3)

    assert (good, total) == (3.0, 3.0), "the newest three, still inside the window"


def test_the_ratio_without_a_window_is_unchanged():
    """Callers that pass no window keep the old meaning: the most recent samples."""
    from examlops.data.governance import slo_sli_ratio

    _sample(1, 1, age=timedelta(days=400))
    _sample(1, 1, age=timedelta(hours=1))

    assert slo_sli_ratio(MODEL, "latency") == (2.0, 2.0)
