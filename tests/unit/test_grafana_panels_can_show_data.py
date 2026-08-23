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

_OURS = re.compile(r"\b((?:examlops|ray_examlops|seanerbus)_[a-z0-9_]+)")
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
        derived.update(n + s for s in _HISTOGRAM_SUFFIXES)
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
    assert "seanerbus_inferences_total" in emitted
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
