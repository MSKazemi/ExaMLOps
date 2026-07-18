"""Unit tests for the Phase 3 Ray Serve resolver and reload paths.

The full deployment requires Ray + MLflow which are heavyweight; these tests
exercise the routing logic on a bare instance constructed via
``object.__new__`` so we never start a Ray actor or talk to MLflow.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Importing the deployment-decorated class triggers Ray imports — fine for
# unit tests because we don't actually run the deployment, just access methods
# off the underlying class.
from serving.ray_serving import app as rs_app  # noqa: E402


def _make_server(
    version_cache_size: int = 8, preload_aliases: list[str] | None = None
) -> rs_app.MultiModelServer:
    """Build a MultiModelServer skeleton without invoking __init__ side-effects."""
    # The @serve.deployment decorator returns a Deployment wrapper; the user-defined
    # class lives at .func_or_class. The wrapper also gives methods a frozen copy
    # of module globals, so per-instance overrides are the only way to inject
    # test state cleanly.
    cls = rs_app.MultiModelServer.func_or_class
    server = object.__new__(cls)
    server._cache_lock = threading.RLock()
    server._hot = {}
    server._version_cache = OrderedDict()
    server._version_cache_size = version_cache_size
    server._preload_aliases = list(preload_aliases or rs_app.PRELOAD_ALIASES)
    server._poll_task = None
    server._replica_id = "test"
    # Stub the metric handles so methods that touch them don't blow up.
    server._req_counter = MagicMock()
    server._latency_hist = MagicMock()
    server._pred_value_hist = MagicMock()
    server._models_gauge = MagicMock()
    server._version_gauge = MagicMock()
    server._reload_counter = MagicMock()
    return server


def _fake_hot_entry(version: str, run_id: str = "rid") -> dict:
    return {"model": MagicMock(), "version": version, "run_id": run_id}


# ── _resolve ─────────────────────────────────────────────────────────────────


class TestResolve:
    def test_default_alias_when_no_alias_or_version(self):
        server = _make_server()
        server._hot[("M", rs_app.MODEL_STAGE)] = _fake_hot_entry("3")

        resolved = server._resolve("M", alias=None, version=None)
        assert resolved["version"] == "3"
        assert resolved["alias"] == rs_app.MODEL_STAGE

    def test_default_alias_missing_returns_404(self):
        server = _make_server()
        # No hot entries at all.
        with pytest.raises(rs_app.HTTPException) as exc_info:
            server._resolve("M", alias=None, version=None)
        assert exc_info.value.status_code == 404

    def test_alias_lookup_hits_hot_set(self):
        server = _make_server()
        server._hot[("M", "Canary")] = _fake_hot_entry("5")
        server._hot[("M", "Production")] = _fake_hot_entry("10")

        resolved = server._resolve("M", alias="Canary", version=None)
        assert resolved["version"] == "5"
        assert resolved["alias"] == "Canary"

    def test_lookup_is_case_insensitive_on_lowercase_registry_name(self):
        # Hot set is keyed on the lowercase MLflow name; an uppercase request
        # (the documented canonical casing) must still resolve, not 404.
        server = _make_server()
        server._hot[("jpcp", rs_app.MODEL_STAGE)] = _fake_hot_entry("3")
        server._hot[("jpcp", "Canary")] = _fake_hot_entry("5")

        assert server._resolve("JPCP", alias=None, version=None)["version"] == "3"
        assert server._resolve("JPCP", alias="Canary", version=None)["version"] == "5"

    def test_alias_lookup_falls_back_to_mlflow_when_not_hot(self):
        server = _make_server()
        # Hot is empty; alias must be loaded on demand.
        fake_mv = SimpleNamespace(version=7)
        fake_pyfunc = SimpleNamespace(metadata=SimpleNamespace(run_id="run-x"))

        with patch.object(rs_app.mlflow, "MlflowClient") as MC:
            MC.return_value.get_model_version_by_alias.return_value = fake_mv
            with patch.object(rs_app.mlflow.pyfunc, "load_model", return_value=fake_pyfunc) as load:
                resolved = server._resolve("M", alias="Staging", version=None)

        load.assert_called_once_with("models:/M@Staging")
        assert resolved["version"] == "7"
        assert resolved["alias"] == "Staging"
        # On-demand alias load should populate the hot set.
        assert server._hot[("M", "Staging")]["version"] == "7"

    def test_alias_lookup_raises_404_when_mlflow_lacks_alias(self):
        server = _make_server()
        with patch.object(rs_app.mlflow, "MlflowClient") as MC:
            MC.return_value.get_model_version_by_alias.side_effect = Exception("not found")
            with pytest.raises(rs_app.HTTPException) as exc_info:
                server._resolve("M", alias="Nonsense", version=None)
        assert exc_info.value.status_code == 404


# ── LRU version cache ────────────────────────────────────────────────────────


class TestVersionCache:
    def test_version_lookup_loads_and_caches(self):
        server = _make_server()
        fake_pyfunc = SimpleNamespace(metadata=SimpleNamespace(run_id="run-1"))

        # Phase 5 adds a get_model_version probe before loading so the loader
        # can pick the right MLflow flavour from the version's framework tag.
        with (
            patch.object(rs_app.mlflow, "MlflowClient") as MC,
            patch.object(rs_app.mlflow.pyfunc, "load_model", return_value=fake_pyfunc) as load,
        ):
            MC.return_value.get_model_version.return_value = SimpleNamespace(version="1", tags={})
            resolved = server._resolve("M", alias=None, version="1")
            # Second call must hit the cache.
            resolved_again = server._resolve("M", alias=None, version="1")

        load.assert_called_once_with("models:/M/1")
        assert resolved["version"] == "1"
        assert resolved_again["version"] == "1"
        assert ("M", "1") in server._version_cache

    def test_version_cache_evicts_least_recently_used(self):
        server = _make_server(version_cache_size=2)
        fake_pyfunc = SimpleNamespace(metadata=SimpleNamespace(run_id="r"))

        with (
            patch.object(rs_app.mlflow, "MlflowClient") as MC,
            patch.object(rs_app.mlflow.pyfunc, "load_model", return_value=fake_pyfunc),
        ):
            MC.return_value.get_model_version.return_value = SimpleNamespace(version="X", tags={})
            server._resolve("M", alias=None, version="1")
            server._resolve("M", alias=None, version="2")
            server._resolve("M", alias=None, version="3")

        # v1 should have been evicted (oldest, never re-touched).
        assert ("M", "1") not in server._version_cache
        assert ("M", "2") in server._version_cache
        assert ("M", "3") in server._version_cache

    def test_version_cache_promotes_on_access(self):
        server = _make_server(version_cache_size=2)
        fake_pyfunc = SimpleNamespace(metadata=SimpleNamespace(run_id="r"))

        with (
            patch.object(rs_app.mlflow, "MlflowClient") as MC,
            patch.object(rs_app.mlflow.pyfunc, "load_model", return_value=fake_pyfunc),
        ):
            MC.return_value.get_model_version.return_value = SimpleNamespace(version="X", tags={})
            server._resolve("M", alias=None, version="1")
            server._resolve("M", alias=None, version="2")
            # Re-touch v1 → v2 becomes the LRU candidate.
            server._resolve("M", alias=None, version="1")
            server._resolve("M", alias=None, version="3")

        assert ("M", "2") not in server._version_cache
        assert ("M", "1") in server._version_cache
        assert ("M", "3") in server._version_cache

    def test_version_not_in_mlflow_returns_404(self):
        server = _make_server()
        with (
            patch.object(rs_app.mlflow, "MlflowClient") as MC,
            patch.object(
                rs_app.mlflow.pyfunc, "load_model", side_effect=Exception("no such version")
            ),
        ):
            MC.return_value.get_model_version.side_effect = Exception("no such version")
            with pytest.raises(rs_app.HTTPException) as exc_info:
                server._resolve("M", alias=None, version="999")
        assert exc_info.value.status_code == 404


# ── _detect_alias_changes ────────────────────────────────────────────────────


class TestDetectAliasChanges:
    def _client_with(self, mapping: dict[tuple[str, str], int | None]):
        """Build an MlflowClient stub from a {(name, alias): version} mapping."""
        names = sorted({n for (n, _) in mapping})
        rms = [SimpleNamespace(name=n) for n in names]

        client = MagicMock()
        client.search_registered_models.return_value = rms

        def by_alias(name, alias):
            v = mapping.get((name, alias))
            if v is None:
                raise Exception("missing")
            return SimpleNamespace(version=v)

        client.get_model_version_by_alias.side_effect = by_alias
        return client

    def test_no_changes_returns_empty(self):
        server = _make_server(preload_aliases=["Production"])
        server._hot[("M", "Production")] = _fake_hot_entry("3")
        client = self._client_with({("M", "Production"): 3})

        assert server._detect_alias_changes(client) == set()

    def test_version_change_is_detected(self):
        server = _make_server(preload_aliases=["Production"])
        server._hot[("M", "Production")] = _fake_hot_entry("3")
        client = self._client_with({("M", "Production"): 4})

        assert server._detect_alias_changes(client) == {"M"}

    def test_new_aliased_model_is_detected(self):
        server = _make_server(preload_aliases=["Production"])
        # Hot is empty; MLflow has a freshly aliased model.
        client = self._client_with({("Brand", "Production"): 1})

        assert server._detect_alias_changes(client) == {"Brand"}


# ── _reload_one_model ────────────────────────────────────────────────────────


class TestReloadOneModel:
    def test_reload_drops_stale_aliases_and_purges_version_cache(self):
        server = _make_server(preload_aliases=["Production", "Canary"])
        server._hot[("M", "Production")] = _fake_hot_entry("3")
        server._hot[("M", "Canary")] = _fake_hot_entry("4")
        server._version_cache[("M", "1")] = _fake_hot_entry("1")
        server._version_cache[("OTHER", "5")] = _fake_hot_entry("5")

        client = MagicMock()

        # Production still exists at v5; Canary alias has been removed in MLflow.
        def by_alias(name, alias):
            if (name, alias) == ("M", "Production"):
                return SimpleNamespace(version=5)
            raise Exception("missing")

        client.get_model_version_by_alias.side_effect = by_alias

        fake_pyfunc = SimpleNamespace(metadata=SimpleNamespace(run_id="r"))

        with (
            patch.object(rs_app.mlflow, "MlflowClient", return_value=client),
            patch.object(rs_app.mlflow.pyfunc, "load_model", return_value=fake_pyfunc),
        ):
            count = server._reload_one_model("M")

        assert count == 1
        assert server._hot[("M", "Production")]["version"] == "5"
        assert ("M", "Canary") not in server._hot  # stale alias dropped
        assert ("M", "1") not in server._version_cache  # purged
        assert ("OTHER", "5") in server._version_cache  # untouched
