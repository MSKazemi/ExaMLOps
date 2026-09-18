"""Model versions are cached locally by content, checked on every use, and bounded (plan P4.3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from serving.ray_serving import artifact_cache as ac  # noqa: E402


class _Registry:
    """A stand-in for `mlflow.artifacts.download_artifacts` that counts downloads."""

    def __init__(self, payload: bytes = b"weights", fail: bool = False) -> None:
        self.payload, self.fail, self.calls = payload, fail, []

    def __call__(self, *, artifact_uri: str, dst_path: str) -> str:
        self.calls.append(artifact_uri)
        if self.fail:
            raise ConnectionError("MLflow is down")
        model = Path(dst_path) / "model"
        (model / "sub").mkdir(parents=True)
        (model / "MLmodel").write_text("flavors: {}\n")
        (model / "sub" / "model.pkl").write_bytes(self.payload + artifact_uri.encode())
        return str(model)


def test_a_version_is_downloaded_once_by_version_then_served_from_disk(tmp_path):
    registry = _Registry()
    cache = ac.ArtifactCache(tmp_path, download=registry)

    first = cache.fetch("jpcp", "7")
    again = cache.fetch("jpcp", "7")

    assert first == again == cache.entry("jpcp", "7")
    assert registry.calls == ["models:/jpcp/7"]  # by version, never by alias
    manifest = json.loads((first / ac.MANIFEST).read_text())
    assert set(manifest["files"]) == {"MLmodel", "sub/model.pkl"}
    assert manifest["digest"] == ac.tree_digest(manifest["files"])


def test_a_corrupted_entry_is_refetched_not_loaded(tmp_path):
    registry = _Registry()
    cache = ac.ArtifactCache(tmp_path, download=registry)
    entry = cache.fetch("jpcp", "7")
    (entry / "sub" / "model.pkl").write_bytes(b"tampered")

    cache.fetch("jpcp", "7")

    assert len(registry.calls) == 2
    assert (entry / "sub" / "model.pkl").read_bytes() != b"tampered"


def test_a_failed_download_leaves_nothing_that_looks_complete(tmp_path):
    cache = ac.ArtifactCache(tmp_path, download=_Registry(fail=True))
    with pytest.raises(ConnectionError):
        cache.fetch("jpcp", "7")
    assert cache.verified_hit("jpcp", "7") is None
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".fetch-")]


def test_a_hit_needs_no_registry_at_all(tmp_path):
    cache = ac.ArtifactCache(tmp_path, download=_Registry())
    cache.fetch("jpcp", "7")
    offline = ac.ArtifactCache(tmp_path, download=_Registry(fail=True))
    assert offline.fetch("jpcp", "7") == cache.entry("jpcp", "7")


def test_least_recently_used_versions_are_evicted_but_never_one_in_use(tmp_path, monkeypatch):
    clock = iter(range(100, 200))
    monkeypatch.setattr(ac.time, "time", lambda: next(clock))
    cache = ac.ArtifactCache(tmp_path, download=_Registry(payload=b"x" * 400))
    first = cache.fetch("jpcp", "1")
    one_entry = sum(p.stat().st_size for p in first.rglob("*") if p.is_file())
    cache.max_bytes = int(one_entry * 2.5)  # room for two versions, not three

    cache.fetch("jpcp", "2")
    cache.fetch("jpcp", "3", keep=[("jpcp", "1")])

    kept = {p.name for p in (tmp_path / "jpcp").iterdir()}
    assert "3" in kept and "1" in kept  # 1 is protected even though it is the oldest
    assert "2" not in kept


# ─── on the Ray load path ────────────────────────────────────────────────────


@pytest.fixture()
def rs_app():
    from serving.ray_serving import app

    return app


def test_the_load_path_serves_the_cached_copy(rs_app, tmp_path, monkeypatch):
    cache = ac.ArtifactCache(tmp_path, download=_Registry())
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", cache)
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "off")

    assert rs_app._verified_uri("jpcp", "7", "models:/jpcp@Production") == str(
        cache.entry("jpcp", "7")
    )


def test_bytes_that_fail_verification_are_neither_loaded_nor_kept(rs_app, tmp_path, monkeypatch):
    cache = ac.ArtifactCache(tmp_path, download=_Registry())
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", cache)
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "enforce")
    monkeypatch.setattr("examlops.supplychain.verify_before_load", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="signature verification failed"):
        rs_app._verified_uri("jpcp", "7", "models:/jpcp/7")
    assert not cache.entry("jpcp", "7").exists()


def test_a_cache_that_cannot_fetch_falls_back_to_the_registry_uri(rs_app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        rs_app, "_ARTIFACT_CACHE", ac.ArtifactCache(tmp_path, download=_Registry(fail=True))
    )
    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "off")
    assert rs_app._verified_uri("jpcp", "7", "models:/jpcp/7") == "models:/jpcp/7"


def test_it_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("RAY_ARTIFACT_CACHE", raising=False)
    assert ac.ArtifactCache.from_env() is None
    monkeypatch.setenv("RAY_ARTIFACT_CACHE", "off")
    assert ac.ArtifactCache.from_env() is None
