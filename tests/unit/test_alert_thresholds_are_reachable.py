"""Every latency alert's threshold is one its histogram can actually report.

``histogram_quantile()`` interpolates inside the bucket that holds the quantile, and an observation
above the largest finite bucket lands in ``+Inf`` — for which it returns that largest finite bound.
So a ``histogram_quantile(...) > T`` alert over a histogram whose buckets stop below ``T`` can never
fire. ``RetrainDurationP99High`` (> 300 s) sat on buckets that stopped at 10 s from the day it was
written. This guard reads each such alert, finds where its histogram's buckets are defined, and
fails if the threshold is out of reach.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "platform" / "infra" / "docker-compose" / "alert_rules.yml"
SOURCES = [
    ROOT / "serving",
    ROOT / "platform" / "services",
    ROOT / "platform" / "clients",
    ROOT / "platform" / "cli" / "src" / "examlops",
]
# Histograms this repository does not define; their buckets belong to the upstream exporter.
EXTERNAL = {"vllm:time_to_first_token_seconds": "vLLM's own exporter"}

_QUANTILE = re.compile(
    r"histogram_quantile\(\s*[\d.]+\s*,.*?rate\(\s*([a-zA-Z_:][\w:]*)_bucket.*\)\s*>\s*([\d.]+)",
    re.S,
)
_BUCKETS = re.compile(r"(?:buckets|boundaries)\s*=\s*[\(\[]([^\)\]]*)[\)\]]")


def _quantile_alerts() -> list[tuple[str, str, float]]:
    out = []
    for group in yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]:
        for rule in group.get("rules", []):
            m = _QUANTILE.search(str(rule.get("expr", "")))
            if "alert" in rule and m:
                out.append((rule["alert"], m.group(1), float(m.group(2))))
    return out


def _largest_bucket(metric: str) -> float | None:
    names = {metric, metric.removeprefix("ray_")}  # Ray prefixes application metrics with ray_
    for base in SOURCES:
        for path in scan_files(base):
            if "tests" in path.parts or "build" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in names:
                at = text.find(f'"{name}"')
                if at < 0:
                    continue
                m = _BUCKETS.search(text, at, at + 600)
                if m:
                    values = [float(v) for v in re.findall(r"[\d.]+(?:e-?\d+)?", m.group(1))]
                    return max(values)
    return None


def test_the_scan_finds_the_latency_alerts():
    names = {a for a, _, _ in _quantile_alerts()}
    assert {"RayServeHighLatencyP99", "RetrainDurationP99High", "VLLMHighTTFT"} <= names


def test_every_latency_threshold_is_reachable():
    problems = []
    for alert, metric, threshold in _quantile_alerts():
        if metric in EXTERNAL:
            continue
        largest = _largest_bucket(metric)
        if largest is None:
            problems.append(f"{alert}: no bucket definition found for {metric}")
        elif largest <= threshold:
            problems.append(
                f"{alert}: fires above {threshold} but {metric}'s largest bucket is {largest}"
            )
    assert not problems, "\n".join(problems)
