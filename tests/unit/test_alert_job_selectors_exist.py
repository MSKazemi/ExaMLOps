"""An alert that selects a job nobody scrapes is silent, and nothing else says so.

Seven "X is down" alerts are `up{job="..."} == 0`. The job name is a string written in one file and
satisfied in another: rename a scrape job in `prometheus.yml`, or mistype one in a new alert, and
the alert selects an empty vector forever. Prometheus is happy, `promtool check rules` is happy,
and the component it watches is simply no longer watched.

A `job` label can also come from a **file_sd target group**, which overrides the scrape config's
`job_name` — verified against a real Prometheus on 2026-09-15: a group labelled `job: vllm` under a
scrape config named `fleet` produces `up{job="vllm"}`. So the fleet's node, DCGM and vLLM exporters
are legitimate job names that appear in no `job_name:` line, and this guard has to accept them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "platform" / "infra" / "docker-compose"
SELECTOR = re.compile(r'job\s*(=~|=)\s*"([^"]+)"')


def _scraped_jobs() -> set[str]:
    prom = yaml.safe_load((D / "prometheus.yml").read_text(encoding="utf-8"))
    jobs = {s["job_name"] for s in prom.get("scrape_configs", [])}
    sd = (ROOT / "platform" / "cli" / "src" / "examlops" / "prometheus_sd.py").read_text()
    return jobs | set(re.findall(r'"job":\s*"(\w+)"', sd))


def _selected_jobs() -> list[tuple[str, str]]:
    rules = yaml.safe_load((D / "alert_rules.yml").read_text(encoding="utf-8"))
    found = []
    for group in rules.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" not in rule:
                continue
            expr = " ".join(str(rule.get("expr", "")).split())
            for op, value in SELECTOR.findall(expr):
                for name in value.split("|") if op == "=~" else [value]:
                    if name:
                        found.append((rule["alert"], name))
    return found


def test_the_inventory_is_not_empty():
    """Both halves must find something, or the check below passes vacuously."""
    assert len(_scraped_jobs()) >= 8, _scraped_jobs()
    assert len(_selected_jobs()) >= 8, _selected_jobs()


def test_every_job_an_alert_selects_is_actually_scraped():
    scraped = _scraped_jobs()
    unknown = sorted({(a, j) for a, j in _selected_jobs() if j not in scraped})
    assert not unknown, (
        "these alerts select a job that neither prometheus.yml scrapes nor a file_sd group sets, "
        f"so they can never fire: {unknown}"
    )
