"""A local, content-addressed cache of model artifacts (ADR 0127 decision 5, plan P4.3).

The serving snapshot tells a restarting replica *what* to serve even when the control plane, the
database and the broker are down; this cache lets it load *the bytes* without MLflow. Each model
version is downloaded once — by version, ``models:/<name>/<version>``, never by alias — into
``<root>/<name>/<version>/`` with a manifest of every file's SHA-256 and the tree's digest.

* **Hit:** the manifest's digests are re-checked against the files before the directory is
  returned, so a cache corrupted on disk (or edited) is refetched, not loaded.
* **Miss:** the artifact is downloaded into a temporary directory, hashed, and moved into place
  atomically; a crash mid-download never leaves a half-populated entry that looks complete.
* **MLflow down:** a hit needs no network at all. A miss raises — the caller keeps its
  last-known-good model.
* **Bounded:** after a fetch, least-recently-used entries beyond ``RAY_ARTIFACT_CACHE_MAX_GB`` are
  evicted, never one the caller says is in use.

Off unless ``RAY_ARTIFACT_CACHE`` names a directory (Compose sets one on a volume).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger("ray_serving.artifact_cache")

MANIFEST = "examlops-cache-manifest.json"
_SAFE = str.maketrans({"/": "_", "\\": "_", ":": "_"})


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(files: dict[str, str]) -> str:
    """One digest for a whole model directory: SHA-256 over sorted ``path:sha256`` lines."""
    lines = "\n".join(f"{rel}:{files[rel]}" for rel in sorted(files))
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): _file_digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    }


class ArtifactCache:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
        download: Callable[..., str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._download = download

    @classmethod
    def from_env(cls) -> ArtifactCache | None:
        root = os.getenv("RAY_ARTIFACT_CACHE", "").strip()
        if not root or root.lower() == "off":
            return None
        max_gb = float(os.getenv("RAY_ARTIFACT_CACHE_MAX_GB", "20"))
        return cls(root, max_bytes=int(max_gb * (1 << 30)) if max_gb > 0 else None)

    def entry(self, name: str, version: str) -> Path:
        return self.root / name.translate(_SAFE) / str(version).translate(_SAFE)

    # -- lookup --------------------------------------------------------------------------
    def verified_hit(self, name: str, version: str) -> Path | None:
        """The cached directory if it is complete and every file matches its manifest."""
        path = self.entry(name, version)
        try:
            manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = manifest.get("files") or {}
        try:
            intact = files and _hash_tree(path) == files
        except OSError:
            intact = False
        if not intact or tree_digest(files) != manifest.get("digest"):
            logger.warning(
                "Cached artifact %s v%s failed its digest check; refetching", name, version
            )
            shutil.rmtree(path, ignore_errors=True)
            return None
        manifest["last_used"] = time.time()
        try:
            (path / MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        except OSError:
            pass  # a read-only cache still serves; LRU order just goes stale
        return path

    def digest(self, name: str, version: str) -> str | None:
        try:
            return json.loads((self.entry(name, version) / MANIFEST).read_text())["digest"]
        except (OSError, ValueError, KeyError):
            return None

    # -- fetch ---------------------------------------------------------------------------
    def fetch(self, name: str, version: str, *, keep: Iterable[tuple[str, str]] = ()) -> Path:
        """The local directory for ``name`` v``version``: cached, or downloaded and cached now."""
        hit = self.verified_hit(name, version)
        if hit is not None:
            return hit
        download = self._download
        if download is None:
            import mlflow  # noqa: PLC0415

            download = mlflow.artifacts.download_artifacts
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=self.root))
        try:
            local = Path(download(artifact_uri=f"models:/{name}/{version}", dst_path=str(staging)))
            files = _hash_tree(local)
            if not files:
                raise RuntimeError(f"models:/{name}/{version} downloaded no files")
            digest = tree_digest(files)
            manifest: dict[str, Any] = {
                "name": name,
                "version": str(version),
                "digest": digest,
                "files": files,
                "fetched_at": time.time(),
                "last_used": time.time(),
            }
            (local / MANIFEST).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            target = self.entry(name, version)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(local, target)  # atomic: an entry is either complete or absent
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        logger.info("Cached %s v%s (%s)", name, version, digest[:19])
        self.evict(keep={*keep, (name, str(version))})
        return target

    def discard(self, name: str, version: str) -> None:
        shutil.rmtree(self.entry(name, version), ignore_errors=True)

    # -- bounds --------------------------------------------------------------------------
    def _entries(self) -> list[tuple[float, int, Path, tuple[str, str]]]:
        out = []
        for manifest_path in self.root.glob(f"*/*/{MANIFEST}"):
            try:
                manifest: dict[str, Any] = json.loads(manifest_path.read_text())
            except (OSError, ValueError):
                continue
            entry = manifest_path.parent
            size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
            key = (str(manifest.get("name")), str(manifest.get("version")))
            out.append((float(manifest.get("last_used") or 0), size, entry, key))
        return out

    def evict(self, *, keep: Iterable[tuple[str, str]] = ()) -> list[Path]:
        """Drop least-recently-used entries until the cache fits ``max_bytes``; never ``keep``."""
        if not self.max_bytes:
            return []
        protected = {(n, str(v)) for n, v in keep}
        entries = sorted(self._entries())
        total = sum(size for _, size, _, _ in entries)
        evicted = []
        for _, size, path, key in entries:
            if total <= self.max_bytes:
                break
            if key in protected:
                continue
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            evicted.append(path)
        return evicted
