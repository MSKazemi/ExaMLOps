"""An alert that cannot fire looks exactly like an alert that never needed to.

`make alerts-check` runs promtool, which proves the rules *parse*. It cannot prove they can
ever match a series, and that is the failure that matters: a rule referencing a job nobody
scrapes, or a liveness gauge that is only ever set to 1, is a permanently silent alarm that
reads on the dashboard as healthy monitoring.

Two properties, each checked in both directions so neither the alert nor the thing it depends
on can drift away from the other without a red test.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "platform" / "infra" / "docker-compose"
_RULES = _COMPOSE / "alert_rules.yml"
_PROM = _COMPOSE / "prometheus.yml"

# Jobs that arrive from file-based service discovery rather than a static target still have a
# `job_name`, so nothing is exempt here; this stays empty on purpose.
_JOBS_NOT_IN_PROMETHEUS_YML: set[str] = set()


def _alerts() -> list[dict]:
    doc = yaml.safe_load(_RULES.read_text())
    return [rule for group in doc["groups"] for rule in group.get("rules", []) if "alert" in rule]


def _scrape_jobs() -> set[str]:
    doc = yaml.safe_load(_PROM.read_text())
    return {sc["job_name"] for sc in doc.get("scrape_configs", [])}


def _jobs_referenced(expr: str) -> set[str]:
    """Every job name an expression selects on, expanding `job=~"a|b"` alternations."""
    found: set[str] = set()
    for op, value in re.findall(r'job\s*(=~|=)\s*"([^"]+)"', expr):
        found.update(value.split("|") if op == "=~" else [value])
    return found


def test_every_job_an_alert_selects_on_is_actually_scraped():
    """`up{job="x"}` is empty — not zero — when nothing scrapes x, so the alert never fires."""
    scraped = _scrape_jobs() | _JOBS_NOT_IN_PROMETHEUS_YML
    dangling = {
        a["alert"]: sorted(_jobs_referenced(str(a["expr"])) - scraped)
        for a in _alerts()
        if _jobs_referenced(str(a["expr"])) - scraped
    }
    assert not dangling, (
        f"these alerts select on jobs prometheus.yml never scrapes, so they can never fire: "
        f"{dangling}. Scraped jobs: {sorted(scraped)}"
    )


def test_no_alert_detects_downtime_with_a_self_reported_liveness_gauge():
    """A process cannot publish its own death.

    A gauge the service sets to 1 while it runs goes *stale*, not to 0, when the service dies —
    so `<service>_up == 0` matches nothing at exactly the moment it is needed. Down-detection
    belongs to Prometheus's own `up`, which the scrape sets to 0 for us.
    """
    offenders = {}
    for a in _alerts():
        expr = str(a["expr"])
        for metric in re.findall(r"\b([a-z_][a-z0-9_]*_up)\s*==\s*0", expr):
            if metric != "up":
                offenders[a["alert"]] = metric
    assert not offenders, (
        f"these alerts test a service-published liveness gauge for 0, which never happens: "
        f'{offenders}. Use up{{job="<job>"}} == 0 instead.'
    )


def test_the_bridge_liveness_gauge_is_in_fact_only_ever_set_to_one():
    """Pins the premise of the test above, so it cannot be dismissed as theoretical."""
    src = (
        Path(__file__).resolve().parents[2] / "platform" / "clients" / "seanerbus_bridge.py"
    ).read_text()
    sets = re.findall(r"_BRIDGE_UP\.set\(([^)]*)\)", src)
    assert sets, "the bridge no longer has a liveness gauge — update this test with it"
    assert all(float(v) == 1.0 for v in sets), (
        f"_BRIDGE_UP is now set to values other than 1.0 ({sets}); if the bridge can publish its "
        "own shutdown, the alert may legitimately test it for 0"
    )


def test_loki_is_monitored_at_all():
    """Loki holds the platform's logs; losing it silently is the worst case for an audit trail."""
    assert "loki" in _scrape_jobs(), (
        "nothing scrapes Loki, so neither LokiDown nor the generic TargetDown can fire for it"
    )


def test_the_documented_scrape_table_matches_prometheus_yml():
    """The guide's job table is what a reader consults before writing an alert.

    It said Prometheus scraped *three* services and listed three, when the config had seven — so
    a reader deciding which job label to select on was reading a list with most of the answers
    missing, which is how a dangling `job=` gets written in the first place.
    """
    doc = (Path(__file__).resolve().parents[2] / "docs" / "components" / "grafana.md").read_text()
    section = doc.split("| Job | Target | Purpose |", 1)[1].split("\n\n", 1)[0]
    documented = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))
    assert documented == _scrape_jobs(), (
        f"docs/components/grafana.md lists {sorted(documented)} but prometheus.yml scrapes "
        f"{sorted(_scrape_jobs())}"
    )
