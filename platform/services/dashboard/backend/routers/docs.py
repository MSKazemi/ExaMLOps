import os
from pathlib import Path
from typing import Any

from auth import require_role
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/docs", dependencies=[Depends(require_role("viewer"))])


def _find_root() -> Path:
    """Find the project root by walking up from this file or via env var.

    Works in both local dev (deep path) and Docker (/app/backend/...).
    """
    env = os.environ.get("EXAMLOPS_DOCS_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "README.md").exists() and (parent / "docs").is_dir():
            return parent
    raise RuntimeError("Cannot locate project root. Set EXAMLOPS_DOCS_ROOT.")


_ROOT = _find_root()

_SECTIONS: list[dict[str, Any]] = [
    {
        "key": "guides",
        "title": "Guides",
        "files": [
            {"path": "README.md", "title": "Project Overview"},
            {"path": "docs/guides/quickstart.md", "title": "Quick Start"},
            {"path": "docs/guides/architecture.md", "title": "Architecture"},
        ],
    },
    {
        "key": "components",
        "title": "Components",
        "files": [
            {"path": "docs/components/prefect.md", "title": "Prefect — Pipelines"},
            {"path": "docs/components/slurm-adapter.md", "title": "Slurm Adapter — HPC"},
            {"path": "docs/components/ray-serve.md", "title": "Ray Serve — Inference"},
            {"path": "docs/components/modelzoo.md", "title": "ModelZoo — Models"},
            {"path": "docs/components/mlflow.md", "title": "MLflow — Tracking"},
            {"path": "docs/components/grafana.md", "title": "Grafana — Monitoring"},
        ],
    },
    {
        "key": "reference",
        "title": "Reference",
        "files": [
            {"path": "docs/reference/api.md", "title": "API Reference"},
            {"path": "docs/reference/env-vars.md", "title": "Environment Variables"},
        ],
    },
]


def _safe_resolve(rel_path: str) -> Path:
    root_str = str(_ROOT)
    resolved = (_ROOT / rel_path).resolve()
    if not (str(resolved) == root_str or str(resolved).startswith(root_str + "/")):
        raise HTTPException(status_code=400, detail="Invalid path")
    return resolved


@router.get("/tree")
async def get_docs_tree() -> list[dict]:
    result = []
    for section in _SECTIONS:
        files = [f for f in section["files"] if (_ROOT / f["path"]).exists()]
        if files:
            result.append({"key": section["key"], "title": section["title"], "files": files})
    return result


@router.get("/content", response_class=PlainTextResponse)
async def get_doc_content(path: str = Query(..., min_length=1, max_length=500)) -> str:
    resolved = _safe_resolve(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if resolved.suffix.lower() not in {".md", ".txt", ".rst"}:
        raise HTTPException(status_code=400, detail="Only text files are served")
    return resolved.read_text(encoding="utf-8")
