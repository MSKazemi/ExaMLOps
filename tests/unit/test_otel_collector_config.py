"""OTel Collector HA metrics/traces config (enterprise-readiness Phase 3, item 3.3).

Structural guard on the collector config: OTLP receivers, tail-based sampling that keeps errors +
slow traces and samples the rest, and the traces→Tempo / metrics→remote-write(Mimir/Thanos)
pipelines — so the long-term-storage wiring can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_DC = Path(__file__).parents[2] / "platform" / "infra" / "docker-compose"


def _load(name):
    return yaml.safe_load((_DC / name).read_text())


def test_otlp_receivers_present():
    cfg = _load("otel-collector-config.yml")
    protocols = cfg["receivers"]["otlp"]["protocols"]
    assert "grpc" in protocols and "http" in protocols


def test_tail_sampling_keeps_errors_and_slow():
    cfg = _load("otel-collector-config.yml")
    policies = {p["name"]: p for p in cfg["processors"]["tail_sampling"]["policies"]}
    assert policies["errors"]["type"] == "status_code"
    assert policies["slow"]["type"] == "latency"
    assert policies["sample-the-rest"]["probabilistic"]["sampling_percentage"] == 5


def test_pipelines_route_traces_and_metrics():
    cfg = _load("otel-collector-config.yml")
    pipes = cfg["service"]["pipelines"]
    assert "tail_sampling" in pipes["traces"]["processors"]
    assert "otlp/tempo" in pipes["traces"]["exporters"]
    assert "prometheusremotewrite" in pipes["metrics"]["exporters"]


def test_remote_write_exporter_configured():
    cfg = _load("otel-collector-config.yml")
    assert "prometheusremotewrite" in cfg["exporters"]
    assert "endpoint" in cfg["exporters"]["prometheusremotewrite"]
