"""GitOps delivery for Kubernetes substrates (ADR 0142 d6, spec-usar-1 R-SUB-25; ADR 0015 d4).

The alternative to a server-side apply: the planned objects are written to a directory an Argo CD
``Application`` or a Flux ``Kustomization`` reconciles, and nothing is sent to an API server.
The layout is fixed so a reviewer can predict it and a reconciler can consume it as-is::

    <root>/<namespace>/kustomization.yaml               # resources: every file below, sorted
    <root>/<namespace>/<kind>-<name>.yaml               # one object per file, e.g.
    <root>/<namespace>/inferenceservice-jpcp.yaml       #   (kind lower-cased)

* **Deterministic** — the same objects produce byte-identical files (sorted keys, block YAML), so
  a re-apply of an unchanged plan is a no-op on disk and in git.
* **Atomic** — each file is written to a temp file in the same directory and renamed over.
* **Contained** — names are validated as DNS-1123 before becoming a path, and every path is checked
  to be inside ``root``; a crafted name cannot write outside it.
* **Commit-ready, not committed** — the platform writes the tree; committing and pushing it is the
  operator's pipeline (the same line the platform draws everywhere between planning a change and
  publishing it). ``apply`` returns the paths it wrote.

``kustomization.yaml`` carries ``namespace:`` so the rendered objects stay namespace-free, exactly as
the server-side-apply path passes ``-n`` rather than stamping ``metadata.namespace`` into a pure
render.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

__all__ = ["GitOpsError", "GitOpsWriter"]

_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
KUSTOMIZATION = "kustomization.yaml"
_MAX_OBJECTS = 64  # a servable renders one or two objects; a flood is a bug, not a plan


class GitOpsError(RuntimeError):
    """The GitOps directory cannot be written as asked."""


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=True, default_flow_style=False, allow_unicode=True)


def _check_label(value: str, what: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _DNS1123.match(value):
        raise GitOpsError(f"{what} {value!r} is not a DNS-1123 label")
    return value


class GitOpsWriter:
    """Write/read/remove rendered objects under ``root/<namespace>/``."""

    def __init__(self, root: str | Path, namespace: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.namespace = _check_label(namespace, "namespace")
        self.dir = self.root / self.namespace

    def _path(self, kind: str, name: str) -> Path:
        _check_label(name, "object name")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", kind or ""):
            raise GitOpsError(f"object kind {kind!r} is not a Kubernetes kind")
        path = (self.dir / f"{kind.lower()}-{name}.yaml").resolve()
        if self.root not in path.parents:
            raise GitOpsError(f"{path} escapes the GitOps root {self.root}")
        return path

    def _write(self, path: Path, text: str) -> bool:
        """Atomically write ``text``; ``False`` when the file already holds exactly that."""
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    def _reindex(self) -> None:
        resources = (
            sorted(p.name for p in self.dir.glob("*.yaml") if p.name != KUSTOMIZATION)
            if self.dir.is_dir()
            else []
        )
        index = self.dir / KUSTOMIZATION
        if not resources:
            index.unlink(missing_ok=True)
            return
        self._write(
            index,
            _dump(
                {
                    "apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization",
                    "namespace": self.namespace,
                    "resources": resources,
                }
            ),
        )

    def write(self, objects: list[dict[str, Any]]) -> list[str]:
        """Write every object (one file each) and refresh the kustomization; return the paths."""
        if len(objects) > _MAX_OBJECTS:
            raise GitOpsError(f"refusing to write {len(objects)} objects (cap {_MAX_OBJECTS})")
        planned = [
            (self._path(str(o.get("kind")), str((o.get("metadata") or {}).get("name"))), o)
            for o in objects
        ]
        written = []
        for path, obj in planned:  # every path validated before the first write
            self._write(path, _dump(obj))
            written.append(str(path))
        self._reindex()
        return written

    def read(self, name: str) -> dict[str, Any] | None:
        """The declared object called ``name`` (any kind), or ``None``."""
        _check_label(name, "object name")
        if not self.dir.is_dir():
            return None
        for path in sorted(self.dir.glob(f"*-{name}.yaml")):
            obj = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and (obj.get("metadata") or {}).get("name") == name:
                return obj
        return None

    def remove(self, name: str) -> list[str]:
        """Remove every declared object called ``name``; idempotent. Returns the removed paths."""
        _check_label(name, "object name")
        if not self.dir.is_dir():
            return []
        removed = []
        for path in sorted(self.dir.glob(f"*-{name}.yaml")):
            obj = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and (obj.get("metadata") or {}).get("name") == name:
                path.unlink()
                removed.append(str(path))
        self._reindex()
        return removed
