"""Static stability: a serving replica restarts and serves with the control plane gone (P4.1).

ADR 0123 invariant 1, as a test. Phase one runs with everything up: the control plane has
published a serving snapshot, and the replica fetched the model version it names. Phase two is the
outage — the platform database, the NATS broker, MLflow and the control plane all unreachable — and
a *new* replica process starts. It must come up serving the same model, from nothing but its own
disk: the last-known-good snapshot (``RAY_SNAPSHOT_CACHE``) and the content-addressed artifact
cache (``RAY_ARTIFACT_CACHE``).

The model is a real scikit-learn estimator in MLflow's own format and the prediction is a real
`predict`; nothing in phase two is mocked except that every network dependency raises.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("sklearn")

from serving.ray_serving import app as rs_app  # noqa: E402
from serving.ray_serving.artifact_cache import ArtifactCache  # noqa: E402
from serving.ray_serving.snapshot import SnapshotReader  # noqa: E402


def _replica(reader: SnapshotReader) -> rs_app.MultiModelServer:
    cls = rs_app.MultiModelServer.func_or_class
    server = object.__new__(cls)
    server._cache_lock = threading.RLock()
    server._hot = {}
    server._version_cache = OrderedDict()
    server._version_cache_size = 8
    server._preload_aliases = ["Production"]
    server._replica_id = "restarted"
    server._snapshot_reader = reader
    server._snapshot_generation = None
    server._snapshot_shadow = None
    server._shadow_cache = {}
    for attr in ("_models_gauge", "_snapshot_gauge", "_reload_counter"):
        setattr(server, attr, MagicMock())
    return server


@pytest.fixture()
def trained_model(tmp_path):
    """A registry stand-in holding one real MLflow sklearn model, and a way to take it away."""
    import mlflow.sklearn
    import numpy as np
    from sklearn.linear_model import LinearRegression

    estimator = LinearRegression().fit(np.array([[0.0], [1.0], [2.0]]), np.array([1.0, 3.0, 5.0]))
    source = tmp_path / "registry" / "jpcp-7"
    mlflow.sklearn.save_model(estimator, str(source))
    state = {"up": True, "downloads": 0}

    def download(*, artifact_uri: str, dst_path: str) -> str:
        if not state["up"]:
            raise ConnectionError(f"MLflow unreachable while fetching {artifact_uri}")
        import shutil

        state["downloads"] += 1
        target = Path(dst_path) / "model"
        shutil.copytree(source, target)
        return str(target)

    return download, state


def test_a_restarted_replica_serves_from_local_state_with_everything_upstream_down(
    tmp_path, monkeypatch, trained_model
):
    from examlops import serving_snapshot
    from examlops.serving_snapshot import digest_of

    download, registry = trained_model
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "off")
    monkeypatch.setattr(rs_app, "_get_serve_aliases_for", lambda name: ["Production"])
    # Nothing may reach a real MLflow in either phase.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")
    snapshot_file = tmp_path / "state" / "snapshot.json"
    artifacts = tmp_path / "state" / "artifacts"

    # ── Phase 1: everything up ────────────────────────────────────────────────────────
    content = {
        "models": {
            "jpcp": {
                "name": "jpcp",
                "aliases": {"Production": {"version": "7", "framework": "sklearn"}},
            }
        },
        "traffic": {},
        "shadow": {},
    }
    generation, _ = serving_snapshot.publish({"schema": 1, "digest": digest_of(content), **content})
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", ArtifactCache(artifacts, download=download))
    healthy = _replica(SnapshotReader(cache_path=snapshot_file))
    assert healthy._apply_newest_snapshot() is True
    assert registry["downloads"] == 1

    # ── Phase 2: the outage, and a brand-new replica process ──────────────────────────
    registry["up"] = False

    def unreachable(*_a, **_k):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(serving_snapshot, "latest", unreachable)
    monkeypatch.setattr(serving_snapshot, "latest_generation", unreachable)
    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    from examlops.events import nats_backend

    monkeypatch.setattr(nats_backend, "shared", unreachable)
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", ArtifactCache(artifacts, download=download))
    reader = SnapshotReader(cache_path=snapshot_file)
    restarted = _replica(reader)

    assert restarted._apply_newest_snapshot() is True
    assert reader.source == "cache"
    assert restarted._snapshot_generation == generation

    import pandas as pd

    resolved = restarted._resolve("jpcp", alias="Production", version=None)
    prediction = resolved["model"].predict(pd.DataFrame({"x": [3.0]}))

    assert resolved["version"] == "7"
    assert float(prediction[0]) == pytest.approx(7.0)  # y = 2x + 1
    assert registry["downloads"] == 1  # phase two fetched nothing


def test_without_local_state_the_same_outage_leaves_the_replica_empty(tmp_path, monkeypatch):
    """The control: what the snapshot and the artifact cache are for."""
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", None)

    def unreachable(*_a, **_k):
        raise ConnectionError("unreachable")

    from examlops import serving_snapshot

    monkeypatch.setattr(serving_snapshot, "latest", unreachable)
    monkeypatch.setattr(serving_snapshot, "latest_generation", unreachable)
    reader = SnapshotReader(cache_path=tmp_path / "never-written.json")
    assert _replica(reader)._apply_newest_snapshot() is False
