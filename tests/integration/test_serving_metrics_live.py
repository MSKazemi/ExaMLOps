"""Ray Serve exports the platform's metrics under the platform's default tracing switch.

Ray 2.55 records metrics through the OpenTelemetry SDK. ``OTEL_SDK_DISABLED=true`` — how the
platform turns tracing off, and its default in Compose and Helm — makes that SDK a no-op, and the
model server's metrics port served only process statistics: no ``ray_examlops_*`` series, no
``ray_serve_*`` series, every serving alert and SLO panel blind. This runs a private local Ray
the way ``serving/ray_serving/app.py`` starts one (the switch set, then
``_prepare_ray_environment()``, then ``ray.init``) and requires a ``ray.util.metrics`` counter to
reach the metrics port. It also pins why the model server republishes its gauges: Ray exports a
gauge only for the report interval in which it was set.

It starts its **own** cluster (``address="local"``, private temp dir, ``RAY_ADDRESS`` removed) and
never touches a Ray already running on the host. Opt-in::

    EXAMLOPS_RAY_LIVE=1 .venv/bin/pytest tests/integration/test_serving_metrics_live.py -v
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "platform" / "cli" / "src"), str(ROOT / "modelzoo")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("EXAMLOPS_RAY_LIVE") != "1", reason="set EXAMLOPS_RAY_LIVE=1"),
]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _series(url: str) -> set[str]:
    body = urllib.request.urlopen(url, timeout=5).read().decode()
    return {
        line.split("{")[0].split(" ")[0]
        for line in body.splitlines()
        if line and not line.startswith("#")
    }


def test_metrics_reach_prometheus_with_tracing_switched_off(monkeypatch):
    ray = pytest.importorskip("ray")
    from ray import serve
    from ray.util.metrics import Counter, Gauge

    from serving.ray_serving import app as server

    monkeypatch.delenv("RAY_ADDRESS", raising=False)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")  # the Compose and Helm default
    server._prepare_ray_environment()

    metrics_port, serve_port = _port(), _port()
    ray.init(
        address="local",
        num_cpus=2,
        include_dashboard=False,
        log_to_driver=False,
        _metrics_export_port=metrics_port,
        _temp_dir=tempfile.mkdtemp(prefix="exa-ray-"),
    )
    try:
        serve.start(http_options={"host": "127.0.0.1", "port": serve_port})

        @serve.deployment(ray_actor_options={"num_cpus": 0.2})
        class Counted:
            def __init__(self) -> None:
                self.requests = Counter(
                    "examlops_metrics_probe_total", description="probe", tag_keys=("route",)
                )
                # Set once, like the model server's gauges were: Ray exports it for one report
                # interval only. And one republished on the model server's cadence.
                Gauge("examlops_probe_set_once", description="probe").set(1)
                refreshed = Gauge("examlops_probe_refreshed", description="probe")

                def refresh() -> None:
                    while True:
                        refreshed.set(1)
                        time.sleep(server.GAUGE_REFRESH_SECONDS)

                threading.Thread(target=refresh, daemon=True).start()

            async def __call__(self, request) -> str:
                self.requests.inc(tags={"route": "probe"})
                return "ok"

        serve.run(Counted.bind(), name="counted", route_prefix="/")
        for _ in range(3):
            urllib.request.urlopen(f"http://127.0.0.1:{serve_port}/", timeout=10).read()

        url = f"http://127.0.0.1:{metrics_port}/metrics"
        deadline, series = time.monotonic() + 60, set()
        while time.monotonic() < deadline:
            series = _series(url)
            if "ray_examlops_metrics_probe_total" in series:
                break
            time.sleep(2)
        assert "ray_examlops_metrics_probe_total" in series, sorted(series)[:20]
        assert any(s.startswith("ray_serve_") for s in series)  # Ray's own serving metrics too

        # Why the model server republishes its gauges. Ray clears a gauge's value each time it is
        # collected ("clear after reading" in Ray's OpenTelemetry recorder), and a worker reports
        # to the metrics agent about every 10 s. So a scraper spaced wider than that, like
        # Prometheus at 15 s, sees a republished gauge every time and a gauge set once never
        # again, while a counter stays.
        seen = [_series(url) for _ in range(4) if not time.sleep(12)]
        assert all("ray_examlops_probe_refreshed" in s for s in seen)
        assert all("ray_examlops_metrics_probe_total" in s for s in seen)
        assert not any("ray_examlops_probe_set_once" in s for s in seen), (
            "Ray now keeps exporting a gauge set once; the model server's refresher "
            "(RAY_GAUGE_REFRESH_SECONDS) may no longer be needed"
        )
    finally:
        serve.shutdown()
        ray.shutdown()
