"""An operator must be told when their SLI query has the shape that dilutes itself.

The platform made this mistake four times in its own alert rules: a numerator selecting *some* of a
counter's label values, divided by a denominator taking *all* of them. Every value outside the
numerator then lowers the measured rate, so unrelated traffic — refused requests, throttled
requests — can hold a burn-rate alert below its threshold during a real incident.

`exa slo set --source prometheus --query <promql>` accepts an arbitrary expression, so an operator
can write exactly that shape and the platform will generate burn-rate alerts on it without a word.
It cannot be *refused*: "share of total" is a legitimate SLI, and the platform cannot know which one
was meant. It can be **said out loud**, with the fix.
"""

from __future__ import annotations

import pytest

from examlops.slo import diluting_denominator

DILUTED = 'sum(rate(http_requests_total{status="error"}[5m])) / sum(rate(http_requests_total[5m]))'


def test_the_shape_the_platform_got_wrong_four_times_is_named():
    warning = diluting_denominator(DILUTED)
    assert warning, "the exact defect shape was not detected"
    assert "http_requests_total" in warning, "the warning must name the metric"
    assert "status" in warning, "the warning must name the label the numerator filters on"


@pytest.mark.parametrize(
    ("label", "query"),
    [
        (
            "denominator names its values",
            'sum(rate(http_requests_total{status="error"}[5m]))'
            ' / sum(rate(http_requests_total{status=~"ok|error"}[5m]))',
        ),
        (
            "different metrics entirely",
            'sum(rate(errors_total{job="a"}[5m])) / sum(rate(requests_total[5m]))',
        ),
        (
            "no filter anywhere — a plain share",
            "sum(rate(http_requests_total[5m])) / sum(rate(all_requests_total[5m]))",
        ),
        ("not a ratio at all", 'avg_over_time(examlops:sli_ratio{model="JPCP"}[30d])'),
        ("empty", ""),
    ],
)
def test_honest_queries_are_not_warned_about(label, query):
    assert diluting_denominator(query) is None, f"false positive on: {label}"


def test_a_filtered_denominator_on_one_of_two_metrics_is_still_caught():
    """The check is per metric: one honest ratio does not excuse a diluted one beside it."""
    query = (
        'sum(rate(a_total{code="5xx"}[5m])) / sum(rate(a_total[5m]))'
        ' + sum(rate(b_total{code="5xx"}[5m])) / sum(rate(b_total{code=~"2xx|5xx"}[5m]))'
    )
    warning = diluting_denominator(query)
    assert warning and "a_total" in warning
    assert "b_total" not in warning, "the honest half must not be reported"


def test_the_warning_says_what_to_do():
    warning = diluting_denominator(DILUTED)
    assert "denominator" in warning.lower()
    # It must point at the fix, not merely complain.
    assert "name" in warning.lower() or "list" in warning.lower()


# ── it must actually reach the operator ───────────────────────────────────────


def _run(args: list[str]):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    return CliRunner().invoke(app, args)


def test_the_operator_sees_the_warning_when_defining_the_slo():
    """Executed through the CLI, not asserted about the function.

    The detector being correct proves nothing if the command never calls it, or calls it after the
    spec is written, or writes it somewhere the operator does not look.
    """
    result = _run(
        ["slo", "set", "JPCP", "availability", "--source", "prometheus", "--query", DILUTED]
    )
    assert result.exit_code == 0, result.output
    assert "dilute" in result.output, (
        f"the operator was told nothing about a self-diluting SLI. Output:\n{result.output}"
    )
    assert "http_requests_total" in result.output


def test_the_spec_is_still_written_because_this_is_a_warning():
    """A warning must not become a refusal: a share-of-total SLI is legitimate."""
    result = _run(["slo", "set", "JPCP", "share-slo", "--source", "prometheus", "--query", DILUTED])
    assert result.exit_code == 0, result.output
    from examlops.data.governance import get_slo_spec

    assert get_slo_spec("JPCP", "share-slo") is not None, "the SLO was refused, not warned about"


def test_an_honest_query_produces_no_warning_through_the_cli():
    """Anti-vacuity for the wiring: the command must not warn about everything."""
    honest = (
        'sum(rate(http_requests_total{status="error"}[5m]))'
        ' / sum(rate(http_requests_total{status=~"ok|error"}[5m]))'
    )
    result = _run(["slo", "set", "JPCP", "honest-slo", "--source", "prometheus", "--query", honest])
    assert result.exit_code == 0, result.output
    assert "dilute" not in result.output, f"false warning:\n{result.output}"


# ── one place decides, every surface presents ─────────────────────────────────


def test_apply_spec_returns_the_warning_so_every_surface_can_show_it():
    """The check belongs at the choke point, not at one call site.

    `exa slo set` was wired first and `exa slo apply` — the *bulk* path, where a file of specs is
    applied at once — was not, nor was the dashboard's `POST /api/slo`. All three go through
    `apply_spec`, so that is where the query is judged; each surface then presents the result in its
    own idiom.
    """
    from examlops.slo import apply_spec

    warnings = apply_spec(
        {
            "model": "JPCP",
            "name": "choke-point",
            "target": 0.99,
            "sli_source": "prometheus",
            "sli_query": DILUTED,
        }
    )
    assert warnings, "apply_spec judged nothing"
    assert any("dilute" in w for w in warnings)


def test_apply_spec_is_quiet_about_an_honest_spec():
    from examlops.slo import apply_spec

    warnings = apply_spec(
        {
            "model": "JPCP",
            "name": "honest-choke",
            "target": 0.99,
            "sli_source": "prometheus",
            "sli_query": 'sum(rate(a_total{s="e"}[5m])) / sum(rate(a_total{s=~"o|e"}[5m]))',
        }
    )
    assert warnings == []


def test_the_bulk_apply_path_warns_too(tmp_path):
    """`exa slo apply` is the path an operator uses for many SLOs at once — and the one I missed."""
    spec_file = tmp_path / "slos.yaml"
    spec_file.write_text(
        "slos:\n"
        "  - model: JPCP\n"
        "    name: bulk-diluted\n"
        "    target: 0.99\n"
        "    sli_source: prometheus\n"
        f"    sli_query: '{DILUTED}'\n"
    )
    result = _run(["slo", "apply", str(spec_file)])
    assert result.exit_code == 0, result.output
    assert "dilute" in result.output, (
        f"a file of SLO specs was applied with no warning at all:\n{result.output}"
    )
    assert "bulk-diluted" in result.output, "the warning must say which spec it is about"


def test_no_caller_of_apply_spec_discards_its_verdict():
    """A new surface must not be able to drop the warning by simply not looking at it.

    Wiring one call site and forgetting the others is the mistake this guard exists for — it is how
    `exa slo apply` and the dashboard were missed. A bare `apply_spec(...)` statement throws the
    verdict away.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    roots = ("platform/cli/src/examlops", "platform/services")
    offenders = []
    for base in roots:
        for path in sorted((root / base).rglob("*.py")):
            if "/tests/" in str(path) or "/test_" in str(path) or ".superpowers" in str(path):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                # a call whose result is thrown away is a bare Expr statement
                if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                    continue
                func = node.value.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "apply_spec":
                    offenders.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    assert not offenders, (
        "these call `apply_spec` and discard its warnings, so a self-diluting SLI would be written "
        f"with nothing said to whoever wrote it: {offenders}"
    )
