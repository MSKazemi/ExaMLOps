"""Scaffold router — generates new model files from bundled templates."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from auth import require_role
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from settings import settings

log = logging.getLogger("dashboard.scaffold")

router = APIRouter(prefix="/scaffold", tags=["scaffold"])

_admin = require_role("admin")

_SCAFFOLD_SCRIPT = Path("/app/tools/scaffold_model.py")


class ScaffoldBody(BaseModel):
    name: str
    task: str = "performance_prediction"
    task_type: str = "regression"
    promotion_metric: str = "rmse"
    promotion_threshold: float = 100.0
    promotion_direction: str = "lower_is_better"
    force: bool = False


def _build_cmd(body: ScaffoldBody, extra: list[str]) -> list[str]:
    cmd = [
        sys.executable, str(_SCAFFOLD_SCRIPT),
        "--name", body.name,
        "--task", body.task,
        "--task-type", body.task_type,
        "--promotion-metric", body.promotion_metric,
        "--promotion-threshold", str(body.promotion_threshold),
        "--promotion-direction", body.promotion_direction,
    ]
    if body.force:
        cmd.append("--force")
    if settings.repo_root:
        cmd += ["--repo-root", settings.repo_root]
    cmd.extend(extra)
    return cmd


@router.post("/preview")
async def preview(body: ScaffoldBody, _=Depends(_admin)) -> dict:
    """Render model files in memory; returns {relative_path: content}."""
    if not _SCAFFOLD_SCRIPT.exists():
        raise HTTPException(503, "Scaffold script not available — rebuild the dashboard image")
    result = subprocess.run(  # noqa: S603
        _build_cmd(body, ["--stdout-json"]),
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise HTTPException(422, result.stderr.strip() or "scaffold preview failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"scaffold output was not valid JSON: {exc}") from exc


@router.post("/create")
async def create(body: ScaffoldBody, _=Depends(_admin)) -> dict:
    """Write model files to the bind-mounted repo. Requires REPO_ROOT."""
    if not _SCAFFOLD_SCRIPT.exists():
        raise HTTPException(503, "Scaffold script not available — rebuild the dashboard image")
    if not settings.repo_root:
        raise HTTPException(
            503,
            "REPO_ROOT not set — add '../../../:/repo:rw' bind mount to the dashboard service in docker-compose.yml",
        )
    result = subprocess.run(  # noqa: S603
        _build_cmd(body, []),
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise HTTPException(422, result.stderr.strip() or "scaffold failed")
    return {"message": result.stdout.strip()}
