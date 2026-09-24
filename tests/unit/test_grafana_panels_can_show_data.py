"""A panel querying a metric nothing emits is indistinguishable from a quiet system.

Grafana renders "No data" for both, so a dashboard can look calm while the thing it is
supposed to show has never been exported at all. These guards hold the provisioned
dashboards to the metrics the code actually defines.

Only metrics *we* emit are checked. `vllm:…`, `dcgm_…`, `node_…` and Prometheus's own `up`
come from exporters outside this tree and cannot be resolved from source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
# Directories that hold *copies* of source rather than source. `platform/cli/build/lib/examlops`
# is a full duplicate of the CLI package, and it is gitignored — so scanning it makes this guard's
# answer depend on whether anyone has run a build, and a metric deleted from the real tree stays
# "emitted" as long as a stale copy survives. Demonstrated: a Counter declared only under `build/`
# was accepted by the inventory. That is the same failure the module docstring is about, one level
# up — a check that cannot fail is worth no more than a panel that cannot show data.
_NOT_SOURCE = {"node_modules", ".venv", "build", "dist", "site-packages", ".git", "__pycache__"}
_DASHBOARDS = (
    _ROOT / "platform" / "infra" / "docker-compose" / "grafana" / "provisioning" / "dashboards"
)

# Ray prefixes every application metric it exports, so the code declares `examlops_x` and
# Prometheus sees `ray_examlops_x`.
_RAY_PREFIX = "ray_"
# Prometheus derives these series from a histogram; the code declares only the base name.
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
# The client library appends `_total` to an exposed Counter whose declared name does not already
# end in it. The older ray/examlops metrics instead bake `_total` into the declared name itself
# (`Counter("examlops_x_total", ...)`), so this derivation is additive to that convention, not a
# replacement for it — the LLM gateway's metrics (`Counter("llm_gateway_requests", ...)`) are the
# first to rely on the client's own auto-suffix instead.
_COUNTER_SUFFIX = ("_total",)

_OURS = re.compile(r"\b((?:examlops|ray_examlops|dataplane-bus|llm_gateway)_[a-z0-9_]+)")
_DECLARED = re.compile(r'(?:Counter|Gauge|Histogram)\(\s*\n?\s*"([a-z][a-z0-9_]*)"')


def _source_files(root: Path) -> list[Path]:
    """Every ``.py`` that is source, not a copy of source — and proof there was something to read."""
    files = [p for p in root.rglob("*.py") if not _NOT_SOURCE & set(p.parts)]
    assert files, (
        f"scanned {root} and found no Python files — the path is stale, not the tree empty"
    )
    return files


def _emitted(root: Path = _ROOT) -> set[str]:
    """Every metric name declared anywhere in the tree, plus the forms Prometheus derives."""
    names: set[str] = set()
    for path in _source_files(root):
        for name in _DECLARED.findall(path.read_text(errors="ignore")):
            names.add(name)
    derived = {_RAY_PREFIX + n for n in names}
    for n in list(names) + list(derived):
        derived.update(n + s for s in _HISTOGRAM_SUFFIXES + _COUNTER_SUFFIX)
    return names | derived


def _queried() -> dict[str, set[str]]:
    """Our own metric names referenced by provisioned dashboards → the files using them."""
    found: dict[str, set[str]] = {}
    for f in sorted(_DASHBOARDS.glob("*.json")):
        doc = json.loads(f.read_text())

        def _panels(ps):
            for p in ps:
                yield p
                yield from _panels(p.get("panels", []))

        for panel in _panels(doc.get("panels", [])):
            for target in panel.get("targets", []):
                for metric in _OURS.findall(target.get("expr", "")):
                    found.setdefault(metric, set()).add(f"{f.name}:{panel.get('title')}")
    return found


def test_every_dashboard_panel_queries_a_metric_something_actually_emits():
    emitted = _emitted()
    dead = {m: sorted(where) for m, where in _queried().items() if m not in emitted}
    assert not dead, (
        "these panels query metrics no Counter/Gauge/Histogram in the tree declares, so they "
        f"render 'No data' forever: {json.dumps(dead, indent=2)}"
    )


def test_the_inventory_itself_is_not_silently_empty():
    """Guards the guard: a broken scan would make the test above vacuously true."""
    emitted = _emitted()
    assert len(emitted) > 20, f"metric inventory looks broken, found only {sorted(emitted)}"
    assert "dataplane_bus_inferences_total" in emitted
    assert "ray_examlops_predict_latency_seconds_bucket" in emitted
    assert _queried(), "no dashboard expressions were parsed at all"


# ── legend labels ─────────────────────────────────────────────────────────────

# Labels Prometheus or Grafana supplies rather than the metric's own declaration.
_AMBIENT_LABELS = {"le", "job", "instance", "cluster", "tenant", "quantile"}

_DECLARED_WITH_LABELS = re.compile(
    r'(?:Counter|Gauge|Histogram)\(\s*\n\s*"([a-z][a-z0-9_]*)",\s*\n\s*"[^"]*",\s*\n\s*\[([^\]]*)\]'
)


def _declared_labels() -> dict[str, set[str]]:
    """metric name → the label set its declaration gives it."""
    out: dict[str, set[str]] = {}
    for path in _source_files(_ROOT):
        for name, labels in _DECLARED_WITH_LABELS.findall(path.read_text(errors="ignore")):
            out[name] = set(re.findall(r'"([a-z_]+)"', labels))
    return out


def test_every_legend_names_a_label_the_metric_actually_carries():
    """A legend interpolating a label the series lacks renders as an unnamed line.

    Grafana does not error on it: the panel draws, the lines are simply indistinguishable, so
    a per-model chart silently stops being per-model. The bridge labels its metrics `model`
    while the control plane uses `model_id`, and the dashboards had mixed the two.
    """
    declared = _declared_labels()
    problems = []
    for f in sorted(_DASHBOARDS.glob("*.json")):
        for match in re.finditer(
            r'"expr"\s*:\s*"([^"]*)"[^}]*?"legendFormat"\s*:\s*"([^"]*)"', f.read_text(), re.S
        ):
            expr, legend = match.group(1), match.group(2)
            for used in re.findall(r"\{\{(\w+)\}\}", legend):
                if used in _AMBIENT_LABELS:
                    continue
                for metric, labels in declared.items():
                    if metric in expr and used not in labels:
                        problems.append(
                            f"{f.name}: legend {{{{{used}}}}} but {metric} has {sorted(labels)}"
                        )
    assert not problems, "\n".join(problems)


def test_the_inventory_reads_source_and_not_copies_of_source(tmp_path):
    """A metric declared only in a build artifact must not count as emitted.

    Built as a fixture tree rather than asserted against this repo on purpose: ``build/`` is
    gitignored, so on a machine that has never run a build — CI, for one — a check phrased against
    the real tree would find nothing to exclude and pass without testing anything. That is the
    failure this module is about, so the guard for it may not have the same shape.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text('Counter("examlops_real_metric", "d")\n')
    for copy_dir in ("build", "dist"):
        d = tmp_path / copy_dir / "lib"
        d.mkdir(parents=True)
        (d / "stale.py").write_text(f'Counter("examlops_{copy_dir}_only_metric", "d")\n')

    emitted = _emitted(tmp_path)

    assert "examlops_real_metric" in emitted
    assert "examlops_build_only_metric" not in emitted, (
        "a metric surviving only in build/ keeps a dead panel looking healthy"
    )
    assert "examlops_dist_only_metric" not in emitted


# ── the complement: a value the code emits that no panel ever shows ───────────

#: `record_retrain(model, dataset, outcome)` — the outcomes the control plane can emit. Derived
#: below from the call sites rather than listed, so a new one cannot be added without this guard
#: noticing. Kept as the metric whose panel set is checked, because it is the one an operator reads
#: to answer "are retrains working".
_RETRAIN_METRIC = "examlops_retrain_requests_total"


def _emitted_retrain_outcomes() -> set[str]:
    """Outcome literals passed to `record_retrain(...)` anywhere in the control plane."""
    import ast

    app = _ROOT / "platform" / "services" / "control_plane" / "app.py"
    outcomes: set[str] = set()
    for node in ast.walk(ast.parse(app.read_text())):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "record_retrain":
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                outcomes.add(arg.value)
    # The first two positionals are model/dataset labels, which are never literals at these call
    # sites; anything literal here is an outcome. Assert that rather than trusting it.
    assert outcomes, "no `record_retrain` call sites found — the sweep is not reading the app"
    return outcomes


def _outcome_panels() -> dict[str, set[str]]:
    """Panels that break the retrain counter down *by outcome* → the outcome values each names.

    Scoped per panel, not pooled across dashboards. Pooling was the first version and a mutant
    survived it: deleting the `throttled` series from the control-plane panel still passed, because
    the overview dashboard happened to show it. "Visible somewhere" is not the promise a panel
    titled *by Outcome* makes.
    """
    panels: dict[str, set[str]] = {}
    for f in sorted(_DASHBOARDS.glob("*.json")):
        doc = json.loads(f.read_text())

        def _walk(ps):
            for p in ps:
                yield p
                yield from _walk(p.get("panels", []))

        for panel in _walk(doc.get("panels", [])):
            title = panel.get("title") or ""
            if "outcome" not in title.lower():
                continue
            values: set[str] = set()
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if _RETRAIN_METRIC not in expr:
                    continue
                for entry in re.findall(r'outcome\s*=~?\s*"([^"]+)"', expr):
                    values.update(entry.split("|"))
            if values:
                panels[f"{f.name}:{title}"] = values
    return panels


def test_every_retrain_outcome_the_code_emits_is_visible_somewhere():
    """A panel headed "by Outcome" must not quietly omit outcomes.

    `throttled` had never appeared on either dashboard, so a rate-limited retrain was invisible to
    the operator reading them — and `dispatched_unrecorded`, added on 2026-09-14, would have joined
    it. Checked **per panel**: an outcome shown on one dashboard does not excuse another.
    """
    emitted = _emitted_retrain_outcomes()
    panels = _outcome_panels()
    assert panels, "no by-outcome panel found — the sweep is not reading the dashboards"
    incomplete = {
        name: sorted(emitted - values) for name, values in panels.items() if emitted - values
    }
    assert not incomplete, (
        "these panels break retrains down by outcome and omit some of them, so an operator cannot "
        f"see those happen: {incomplete}"
    )


def test_the_success_rate_denominator_names_its_outcomes():
    """A success *rate* whose denominator is the bare counter changes meaning when a label is added.

    Found by consequence: adding the `dispatched_unrecorded` outcome silently entered this panel's
    denominator, so a retrain that was dispatched, lost its bookkeeping write and then succeeded on
    retry showed as **50%** success. The denominator must name the outcomes it counts.
    """
    offenders = []
    for f in sorted(_DASHBOARDS.glob("*.json")):
        doc = json.loads(f.read_text())

        def _panels(ps):
            for p in ps:
                yield p
                yield from _panels(p.get("panels", []))

        for panel in _panels(doc.get("panels", [])):
            title = (panel.get("title") or "").lower()
            if "rate" not in title or "success" not in title:
                continue
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if _RETRAIN_METRIC not in expr:
                    continue
                # every occurrence of the metric must carry an `outcome` matcher
                bare = re.findall(rf"{_RETRAIN_METRIC}(?!\s*\{{)", expr)
                if bare:
                    offenders.append(f"{f.name}:{panel.get('title')}")
    assert not offenders, (
        "a success-rate panel divides by the unfiltered counter, so adding an outcome label "
        f"changes what it reports: {sorted(set(offenders))}"
    )


# The 30-day 0.5 % objective these serving panels are written against, and the metric they read.
_ERROR_BUDGET = 0.005
_PREDICT_METRIC = "ray_examlops_predict_requests_total"


def _burn_panels() -> list[tuple[str, str, str]]:
    """(file, title, expr) for every panel presenting a burn rate."""
    found = []
    for f in sorted(_DASHBOARDS.glob("*.json")):
        doc = json.loads(f.read_text())

        def _panels(ps):
            for p in ps:
                yield p
                yield from _panels(p.get("panels", []))

        for panel in _panels(doc.get("panels", [])):
            title = panel.get("title") or ""
            if "burn" not in title.lower():
                continue
            for target in panel.get("targets", []):
                if _PREDICT_METRIC in target.get("expr", ""):
                    found.append((f.name, title, " ".join(target["expr"].split())))
    return found


def _evaluate_burn(expr: str, errors_per_sec: float, total_per_sec: float) -> float:
    """Substitute a traffic mix into a burn-rate expression and finish the arithmetic in Python.

    The same technique as `test_burn_rate_is_a_ratio.py`, and for the same reason: the broken form
    contains every token the correct one does, so only evaluating it tells them apart.
    """

    def _stream(match: re.Match[str]) -> str:
        selector = match.group(0)
        rate = total_per_sec if "success" in selector else errors_per_sec
        return f"__S__[{rate}]"

    expr = re.sub(re.escape(_PREDICT_METRIC) + r"\{[^}]*\}\[[0-9a-z]+\]", _stream, expr)
    expr = re.sub(re.escape(_PREDICT_METRIC) + r"\[[0-9a-z]+\]", f"__S__[{total_per_sec}]", expr)
    expr = re.sub(r"sum\((?:rate|increase)\(__S__\[([0-9.e+-]+)\]\)\)", r"\1", expr)
    assert _PREDICT_METRIC not in expr, f"unsubstituted terms left in: {expr}"
    return float(eval(expr, {"__builtins__": {}}, {"clamp_min": max}))  # noqa: S307


def test_a_burn_rate_panel_shows_a_burn_rate():
    """A panel called "burn rate" must show the dimensionless multiple, like the alert does.

    Found on 2026-09-14 while checking that the burn-rate *alert* fix had reached the dashboards.
    It had not. The panel divided an error **rate** by "budget per hour"
    (`sum(rate(errors[1h])) / (0.005 / (30*24))`) — errors/second over fraction/hour — which is the
    same dimensional error the alerts were fixed for in 54a9dcc, left behind in the panel.

    Two consequences, both visible on the NOC wall. A service comfortably inside its SLO (0.1 %
    errors, a true burn of 0.2x) rendered **1440**, well past the panel's own red threshold of
    14.4, so the panel was permanently red. And with no denominator the number tracked traffic
    volume rather than reliability: two services at an identical 0.2x burn showed 1440 and 144000
    purely because one served more requests. A panel that is always red teaches operators to
    ignore it.
    """
    panels = _burn_panels()
    assert panels, "no burn-rate panel found — this guard would pass vacuously"
    for fname, title, expr in panels:
        healthy = _evaluate_burn(expr, errors_per_sec=0.01, total_per_sec=10.0)  # 0.1 % errors
        outage = _evaluate_burn(expr, errors_per_sec=1.0, total_per_sec=10.0)  # 10 % errors
        busy = _evaluate_burn(expr, errors_per_sec=1.0, total_per_sec=1000.0)  # 0.1 % errors
        assert healthy == pytest.approx(0.2, rel=0.01), (
            f"{fname}:{title} shows {healthy:g} for a service at 0.1 % errors against a "
            f"{_ERROR_BUDGET} budget — a true burn rate of 0.2x."
        )
        assert outage == pytest.approx(20.0, rel=0.01), (
            f"{fname}:{title} shows {outage:g} for a 10 % error rate, which is a 20x burn."
        )
        assert busy == pytest.approx(healthy, rel=0.01), (
            f"{fname}:{title} shows {busy:g} at high traffic and {healthy:g} at low traffic for "
            "the same 0.1 % error rate — it is measuring volume, not reliability."
        )
