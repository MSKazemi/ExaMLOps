"""ModelZoo stats + datasets — reads live data from the GitLab repository API."""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

import httpx
from auth import require_role
from database import get_db
from fastapi import APIRouter, Depends, HTTPException
from models import DashboardConfig
from settings import settings
from sqlalchemy.ext.asyncio import AsyncSession

from routers.config import get_decrypted_secret

logger = logging.getLogger("dashboard.modelzoo")

router = APIRouter(prefix="/modelzoo", tags=["modelzoo"])


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


# Paths within the standalone modelzoo repo (e.g. software/modelzoo on GitLab)
_TASKS_PATH = "modelzoo/models/tasks"
_DATASETS_PATH = "modelzoo/datasets"
_DATASET_EXCLUDES = {"__init__.py", "_backends.py"}


# ── Config helpers ────────────────────────────────────────────────────────────


async def _get_plain(db: AsyncSession, key: str) -> str | None:
    row = await db.get(DashboardConfig, key)
    return row.value if row and row.value else None


async def _get_credentials(db: AsyncSession) -> tuple[str | None, str | None, str]:
    """Return (token, project_id, gitlab_url) — DB value overrides env default."""
    token, project_id, gitlab_url_db = await asyncio.gather(
        get_decrypted_secret(db, "gitlab_token"),
        _get_plain(db, "gitlab_project_id"),
        _get_plain(db, "gitlab_url"),
    )
    return (
        token or settings.gitlab_token,
        project_id or settings.gitlab_project_id,
        gitlab_url_db or settings.gitlab_url,
    )


# ── GitLab HTTP ───────────────────────────────────────────────────────────────


async def _gitlab_get(
    gitlab_url: str,
    token: str,
    project_id: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    base = gitlab_url.rstrip("/")
    url = f"{base}/api/v4/projects/{urllib.parse.quote(project_id, safe='')}/repository/{path}"
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:  # noqa: S501
        r = await client.get(url, headers=headers, params=params or {})
    if r.status_code == 401:
        raise HTTPException(status_code=502, detail="GitLab token invalid or expired (401)")
    if r.status_code == 403:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        scope_hint = body.get("error_description", "")
        raise HTTPException(
            status_code=502,
            detail=f"GitLab access denied (403) — {scope_hint or 'check token scope (read_repository) and role (Reporter+)'}",
        )
    if r.status_code == 404:
        raise HTTPException(
            status_code=502, detail=f"GitLab path not found — check project ID and branch: {url}"
        )
    r.raise_for_status()
    return r.json()


async def _gitlab_raw(
    gitlab_url: str,
    token: str,
    project_id: str,
    file_path: str,
    branch: str,
) -> str:
    encoded_path = urllib.parse.quote(file_path, safe="")
    encoded_id = urllib.parse.quote(project_id, safe="")
    base = gitlab_url.rstrip("/")
    url = f"{base}/api/v4/projects/{encoded_id}/repository/files/{encoded_path}/raw"
    headers = {"PRIVATE-TOKEN": token}
    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:  # noqa: S501
        r = await client.get(url, headers=headers, params={"ref": branch})
    return r.text if r.is_success else ""


def _file_url(gitlab_url: str, project_id: str, file_path: str, branch: str) -> str:
    base = gitlab_url.rstrip("/")
    pid = urllib.parse.quote(project_id, safe="/")
    return f"{base}/{pid}/-/blob/{branch}/{file_path}"


def _tree_url(gitlab_url: str, project_id: str, dir_path: str, branch: str) -> str:
    base = gitlab_url.rstrip("/")
    pid = urllib.parse.quote(project_id, safe="/")
    return f"{base}/{pid}/-/tree/{branch}/{dir_path}"


# ── Tree parsing ──────────────────────────────────────────────────────────────


def _parse_models(items: list[dict], gitlab_url: str, project_id: str, branch: str) -> list[dict]:
    out = []
    for item in items:
        if item.get("type") != "tree":
            continue
        rel = item["path"][len(_TASKS_PATH) + 1 :]
        parts = rel.split("/")
        if len(parts) == 2:
            task_cat, model_dir = parts
            out.append(
                {
                    "name": model_dir,
                    "task_category": task_cat,
                    "dir_path": item["path"],
                    "file_url": _tree_url(gitlab_url, project_id, item["path"], branch),
                }
            )
    return sorted(out, key=lambda x: x["name"])


def _count_models(items: list[dict]) -> tuple[int, list[str]]:
    task_cats: set[str] = set()
    model_count = 0
    for item in items:
        if item.get("type") != "tree":
            continue
        rel = item["path"][len(_TASKS_PATH) + 1 :]
        parts = rel.split("/")
        if len(parts) == 1:
            task_cats.add(parts[0])
        elif len(parts) == 2:
            model_count += 1
    return model_count, sorted(task_cats)


def _parse_dataset_files(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        name = item.get("name", "")
        if (
            item.get("type") == "blob"
            and name.endswith(".py")
            and name not in _DATASET_EXCLUDES
            and "/common/" not in item.get("path", "")
        ):
            out.append({"filename": name, "path": item["path"]})
    return out


_CLASS_RE = re.compile(r"class\s+(\w+Dataset)\s*[\(:]")


def _extract_class_name(source: str, filename: str) -> str:
    m = _CLASS_RE.search(source)
    if m:
        return m.group(1)
    stem = filename[:-3]
    return (
        "".join(p.upper() if len(p) <= 3 else p.capitalize() for p in stem.split("_")) + "Dataset"
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_modelzoo_stats(
    _claims: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token, project_id, gitlab_url = await _get_credentials(db)
    if not token or not project_id:
        return {"configured": False}

    branch = settings.gitlab_branch
    try:
        tasks_items, datasets_items, commits = await asyncio.gather(
            _gitlab_get(
                gitlab_url,
                token,
                project_id,
                "tree",
                {"path": _TASKS_PATH, "recursive": "true", "per_page": 100, "ref": branch},
            ),
            _gitlab_get(
                gitlab_url,
                token,
                project_id,
                "tree",
                {"path": _DATASETS_PATH, "per_page": 100, "ref": branch},
            ),
            _gitlab_get(
                gitlab_url,
                token,
                project_id,
                "commits",
                {"path": _TASKS_PATH, "per_page": 1, "ref_name": branch},
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("GitLab fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"GitLab unreachable: {exc}") from exc

    models_count, task_cats = _count_models(tasks_items)
    dataset_files = _parse_dataset_files(datasets_items)

    last_commit: dict | None = None
    if commits:
        c = commits[0]
        last_commit = {
            "sha": c.get("short_id") or (c.get("id") or "")[:8],
            "message": (c.get("title") or c.get("message") or "")[:120],
            "author_name": c.get("author_name", ""),
            "committed_date": c.get("committed_date") or c.get("created_at"),
        }

    return {
        "configured": True,
        "models_count": models_count,
        "datasets_count": len(dataset_files),
        "task_categories": task_cats,
        "last_commit": last_commit,
        "branch": branch,
        "repo_url": _tree_url(gitlab_url, project_id, "", branch).rstrip("/"),
    }


@router.get("/datasets")
async def get_modelzoo_datasets(
    _claims: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    token, project_id, gitlab_url = await _get_credentials(db)
    if not token or not project_id:
        return []

    branch = settings.gitlab_branch
    try:
        items = await _gitlab_get(
            gitlab_url,
            token,
            project_id,
            "tree",
            {"path": _DATASETS_PATH, "per_page": 100, "ref": branch},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitLab unreachable: {exc}") from exc

    dataset_files = _parse_dataset_files(items)
    if not dataset_files:
        return []

    sources = await asyncio.gather(
        *[_gitlab_raw(gitlab_url, token, project_id, f["path"], branch) for f in dataset_files],
        return_exceptions=True,
    )

    out = []
    for f, src in zip(dataset_files, sources):
        raw = src if isinstance(src, str) else ""
        class_name = _extract_class_name(raw, f["filename"])
        out.append(
            {
                "class_name": class_name,
                "filename": f["filename"],
                "file_path": f["path"],
                "file_url": _file_url(gitlab_url, project_id, f["path"], branch),
            }
        )

    return sorted(out, key=lambda x: x["class_name"])


@router.get("/models")
async def get_modelzoo_models(
    _claims: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    token, project_id, gitlab_url = await _get_credentials(db)
    if not token or not project_id:
        return []

    branch = settings.gitlab_branch
    try:
        items = await _gitlab_get(
            gitlab_url,
            token,
            project_id,
            "tree",
            {"path": _TASKS_PATH, "recursive": "true", "per_page": 100, "ref": branch},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitLab unreachable: {exc}") from exc

    return _parse_models(items, gitlab_url, project_id, branch)


@router.post(
    "/trigger-pipeline",
    summary="Trigger the modelzoo GitLab CI pipeline (admin only)",
)
async def trigger_pipeline(
    _claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    pipeline_token = await get_decrypted_secret(db, "gitlab_pipeline_token")
    project_id = await _get_plain(db, "gitlab_project_id")
    gitlab_url = await _get_plain(db, "gitlab_url") or settings.gitlab_url
    branch = await _get_plain(db, "gitlab_branch") or settings.examlops_repo_branch

    # Fall back to AI-Production pipeline trigger env vars when DB token is absent.
    # This triggers the ai-production CI pipeline (test:modelzoo job) rather than
    # the modelzoo project's own pipeline.
    if not pipeline_token and settings.ai_prod_pipeline_trigger_token:
        pipeline_token = settings.ai_prod_pipeline_trigger_token
        if not project_id and settings.ai_prod_gitlab_project_id:
            project_id = settings.ai_prod_gitlab_project_id
        branch = "main"

    # Last resort: use the personal access token (GITLAB_TOKEN) with the regular
    # pipeline-create API. This avoids needing a separate pipeline trigger token
    # for local dev where only a PAT is available.
    use_pat_fallback = not pipeline_token and bool(settings.gitlab_token)
    if use_pat_fallback:
        project_id = project_id or settings.gitlab_project_id
        if not project_id:
            raise HTTPException(
                status_code=503,
                detail=(
                    "GitLab project ID not configured. "
                    "Add gitlab_project_id in Config or set GITLAB_PROJECT_ID in .env."
                ),
            )
    elif not pipeline_token:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitLab pipeline token not configured. "
                "Set AI_PROD_PIPELINE_TRIGGER_TOKEN in .env or add a token in Config."
            ),
        )
    elif not project_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitLab project ID not configured. "
                "Set AI_PROD_GITLAB_PROJECT_ID in .env or add it in Config."
            ),
        )

    encoded_project_id = urllib.parse.quote(project_id, safe="")

    async with _http_client() as http:
        if use_pat_fallback:
            resp = await http.post(
                f"{gitlab_url}/api/v4/projects/{encoded_project_id}/pipeline",
                json={"ref": branch},
                headers={"PRIVATE-TOKEN": settings.gitlab_token},
            )
        else:
            resp = await http.post(
                f"{gitlab_url}/api/v4/projects/{encoded_project_id}/trigger/pipeline",
                data={"token": pipeline_token, "ref": branch},
            )

    if resp.status_code == 401:
        raise HTTPException(status_code=502, detail="GitLab: authentication failed — check token")
    if resp.status_code == 404:
        raise HTTPException(status_code=502, detail="GitLab: project not found")
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"GitLab error: {resp.text[:200]}")

    data = resp.json()
    return {
        "pipeline_id": data["id"],
        "status": data["status"],
        "web_url": data["web_url"],
    }


@router.get(
    "/pipeline-status/{pipeline_id}",
    summary="Poll GitLab for pipeline status",
)
async def pipeline_status(
    pipeline_id: int,
    _claims: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    token, project_id, gitlab_url = await _get_credentials(db)
    if not project_id:
        raise HTTPException(status_code=503, detail="gitlab_project_id not configured")

    headers = {"PRIVATE-TOKEN": token} if token else {}
    encoded_project_id = urllib.parse.quote(project_id, safe="")
    async with _http_client() as http:
        resp = await http.get(
            f"{gitlab_url}/api/v4/projects/{encoded_project_id}/pipelines/{pipeline_id}",
            headers=headers,
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitLab error: {resp.text[:200]}")

    data = resp.json()
    return {
        "status": data["status"],
        "web_url": data["web_url"],
        "duration_seconds": data.get("duration"),
        "created_at": data.get("created_at"),
        "finished_at": data.get("finished_at"),
    }
