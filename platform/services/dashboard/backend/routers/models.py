"""Model detail endpoints. Reads from control-plane + MLflow + dashboard DB."""
from __future__ import annotations

import asyncio
import json as _json
import uuid

import httpx
from auth import require_role
from control_plane_client import ControlPlaneClient
from database import get_db
from external_links import LinkInputs, build_links
from fastapi import APIRouter, Body, Depends, File, HTTPException, Response, UploadFile, status
from frontmatter import parse_readme
from models import ModelDocImage, ModelDocOverride
from pydantic import BaseModel
from settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from storage import ImageStorage

router = APIRouter(prefix="/models")


def _control_plane() -> ControlPlaneClient:
    """Module-level factory so tests can monkeypatch this with a fake."""
    return ControlPlaneClient(base_url=settings.control_plane_url)


@router.get("/registry")
async def list_registry(
    _claims: dict = Depends(require_role("viewer")),
) -> list[dict]:
    cp = _control_plane()
    names = await cp.list_model_names()
    out: list[dict] = []
    for name in names:
        try:
            meta = await cp.get_meta(name)
        except KeyError:
            continue
        out.append({
            "name": meta["name"],
            "task_type": meta["task_type"],
            "supported_datasets": meta["supported_datasets"],
        })
    return out


def _mlflow_client() -> httpx.AsyncClient:
    """Module-level factory; tests monkeypatch this with a MockTransport."""
    return httpx.AsyncClient(base_url=settings.mlflow_url, timeout=5.0)


def _pick_alias(aliases: list[str]) -> str | None:
    """Return the lifecycle alias most relevant to ranking (P > C > S > Archived)."""
    for a in ("Production", "Canary", "Staging", "Archived"):
        if a in aliases:
            return a
    return None


@router.get("/{name}/versions")
async def list_versions(
    name: str,
    _claims: dict = Depends(require_role("viewer")),
) -> list[dict]:
    cp = _control_plane()
    try:
        meta = await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc

    model_id: str = (meta.get("promotion") or {}).get("model_id") or name

    async def _fetch_metrics(mlflow: httpx.AsyncClient, run_id: str) -> dict[str, float]:
        if not run_id:
            return {}
        try:
            r = await mlflow.get("/api/2.0/mlflow/runs/get", params={"run_id": run_id})
            if r.status_code != 200:
                return {}
            metrics_list = r.json().get("run", {}).get("data", {}).get("metrics", [])
            return {m["key"]: m["value"] for m in metrics_list}
        except Exception:
            return {}

    async with _mlflow_client() as mlflow:
        # Fetch registered model to get alias→version mapping.
        reg_r = await mlflow.get(
            "/api/2.0/mlflow/registered-models/get",
            params={"name": model_id},
        )
        version_aliases: dict[str, list[str]] = {}
        _reg_aliases_loaded = False
        if reg_r.status_code == 200:
            _reg_aliases_loaded = True
            for entry in reg_r.json().get("registered_model", {}).get("aliases", []):
                v = str(entry["version"])
                version_aliases.setdefault(v, []).append(entry["alias"])

        # Fetch all model versions.
        ver_r = await mlflow.get(
            "/api/2.0/mlflow/model-versions/search",
            params={"filter": f"name='{model_id}'"},
        )
        if ver_r.status_code == 404:
            return []
        ver_r.raise_for_status()
        raw = ver_r.json().get("model_versions", [])

        # Fetch metrics for all versions concurrently.
        run_ids = [v.get("run_id", "") for v in raw]
        all_metrics = await asyncio.gather(*[_fetch_metrics(mlflow, rid) for rid in run_ids])

    out: list[dict] = []
    for v, metrics in zip(raw, all_metrics):
        ver_str = str(v.get("version", ""))
        # When the registered-model endpoint was reachable, trust its alias list
        # (missing key → no aliases). Only fall back to raw version data when the
        # endpoint was unavailable (e.g. model not yet registered).
        aliases = version_aliases.get(ver_str, []) if _reg_aliases_loaded else v.get("aliases", [])
        alias = _pick_alias(aliases)
        tags = v.get("tags", [])
        framework = next(
            (t["value"] for t in tags if t.get("key") == "framework"),
            "sklearn",
        )
        out.append({
            "version": v.get("version"),
            "run_id": v.get("run_id"),
            "alias": alias,
            "aliases": aliases,
            "framework": framework,
            "metrics": metrics,
            "created_at": v.get("creation_timestamp"),
            "updated_at": v.get("last_updated_timestamp"),
        })
    return out


VALID_ALIASES: frozenset[str] = frozenset({"Staging", "Canary", "Production", "Archived"})


class AliasBody(BaseModel):
    alias: str


async def _fetch_versions_for_model(name: str, claims: dict) -> list[dict]:
    return await list_versions(name, _claims=claims)


@router.put("/{name}/versions/{version}/alias")
async def set_version_alias(
    name: str,
    version: str,
    body: AliasBody,
    claims: dict = Depends(require_role("admin")),
) -> list[dict]:
    if body.alias not in VALID_ALIASES:
        raise HTTPException(
            status_code=422,
            detail=f"alias must be one of {sorted(VALID_ALIASES)}",
        )
    cp = _control_plane()
    try:
        meta = await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc
    model_id: str = (meta.get("promotion") or {}).get("model_id") or name

    async with _mlflow_client() as mlflow:
        prev_version: str | None = None
        if body.alias == "Production":
            prev_r = await mlflow.get(
                "/api/2.0/mlflow/registered-models/alias",
                params={"name": model_id, "alias": "Production"},
            )
            if prev_r.status_code == 200:
                prev_version = str(prev_r.json()["model_version"]["version"])
                if prev_version == version:
                    prev_version = None  # no demotion needed — already the holder

        elif body.alias == "Archived":
            for a in ("Staging", "Canary", "Production"):
                chk = await mlflow.get(
                    "/api/2.0/mlflow/registered-models/alias",
                    params={"name": model_id, "alias": a},
                )
                if chk.status_code == 200:
                    mv = chk.json().get("model_version", {})
                    if str(mv.get("version", "")) == version:
                        await mlflow.request(
                            "DELETE",
                            "/api/2.0/mlflow/registered-models/alias",
                            content=_json.dumps({"name": model_id, "alias": a}).encode(),
                            headers={"Content-Type": "application/json"},
                        )

        set_r = await mlflow.post(
            "/api/2.0/mlflow/registered-models/alias",
            json={"name": model_id, "alias": body.alias, "version": version},
        )
        if set_r.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"MLflow error: {set_r.text[:200]}")

        # Demote after promotion is confirmed successful.
        # No need to DELETE Production from the old holder — the POST above already
        # moved the alias in MLflow. Just mark the old holder as Archived.
        if body.alias == "Production" and prev_version is not None:
            await mlflow.post(
                "/api/2.0/mlflow/registered-models/alias",
                json={"name": model_id, "alias": "Archived", "version": prev_version},
            )

    return await _fetch_versions_for_model(name, claims)


@router.delete("/{name}/versions/{version}/alias/{alias}")
async def delete_version_alias(
    name: str,
    version: str,
    alias: str,
    claims: dict = Depends(require_role("admin")),
) -> list[dict]:
    if alias not in VALID_ALIASES:
        raise HTTPException(
            status_code=422,
            detail=f"alias must be one of {sorted(VALID_ALIASES)}",
        )
    cp = _control_plane()
    try:
        meta = await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc
    model_id: str = (meta.get("promotion") or {}).get("model_id") or name

    async with _mlflow_client() as mlflow:
        # Verify alias exists and belongs to the specified version before deleting.
        chk_r = await mlflow.get(
            "/api/2.0/mlflow/registered-models/alias",
            params={"name": model_id, "alias": alias},
        )
        if chk_r.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Alias {alias!r} not found on model {name!r}")
        if chk_r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MLflow error: {chk_r.text[:200]}")
        holder_version = str(chk_r.json()["model_version"]["version"])
        if holder_version != version:
            raise HTTPException(
                status_code=409,
                detail=f"Alias {alias!r} belongs to version {holder_version}, not {version}",
            )

        del_r = await mlflow.request(
            "DELETE",
            "/api/2.0/mlflow/registered-models/alias",
            content=_json.dumps({"name": model_id, "alias": alias}).encode(),
            headers={"Content-Type": "application/json"},
        )
        if del_r.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"MLflow error: {del_r.text[:200]}")

    return await _fetch_versions_for_model(name, claims)


def _image_storage() -> ImageStorage:
    """Module-level factory; tests monkeypatch this with a stub if needed."""
    return ImageStorage(
        endpoint_url=settings.minio_url,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.dashboard_minio_bucket,
    )


@router.get("/{name}")
async def get_model_detail(
    name: str,
    _claims: dict = Depends(require_role("viewer")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cp = _control_plane()
    try:
        meta = await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc

    fs_text, fs_sha = await cp.get_readme(name)
    fm, fs_body, warnings = parse_readme(fs_text)

    override_row = (await db.execute(
        select(ModelDocOverride).where(ModelDocOverride.model_name == name)
    )).scalar_one_or_none()

    if override_row is not None:
        body = override_row.body
        upstream_drift = override_row.fs_sha != fs_sha
        source = "override"
        updated_at = override_row.updated_at.isoformat() if override_row.updated_at else None
    else:
        body = fs_body
        upstream_drift = False
        source = "filesystem" if fs_text else "empty"
        updated_at = None

    versions = await list_versions(name, _claims=_claims)
    stages: dict[str, dict | None] = {"production": None, "canary": None, "staging": None}
    for v in versions:
        a = (v.get("alias") or "").lower()
        if a in stages and stages[a] is None:
            stages[a] = v

    primary_dataset = (meta["supported_datasets"] or [None])[0]
    prod_run = (stages["production"] or {}).get("run_id") if stages["production"] else None
    prod_version = (stages["production"] or {}).get("version") if stages["production"] else None

    paper_url = None
    if isinstance(fm.paper, dict):
        paper_url = fm.paper.get("url")

    mlflow_model_id = (meta.get("promotion") or {}).get("model_id") or name

    links = build_links(LinkInputs(
        model_name=name,
        mlflow_model_id=mlflow_model_id,
        model_path_in_repo=meta["path_in_repo"],
        primary_dataset=primary_dataset,
        run_id=prod_run,
        version=prod_version,
        paper_url=paper_url,
        public_mlflow_url=settings.public_mlflow_url,
        public_prefect_url=settings.public_prefect_url,
        public_ray_serve_url=settings.public_ray_serve_url,
        grafana_loki_explore_url=settings.grafana_loki_explore_url,
        public_control_plane_url=settings.public_control_plane_url,
        examlops_repo_url=settings.examlops_repo_url,
        examlops_repo_branch=settings.examlops_repo_branch,
    ))

    images: list[dict] = []
    for filename in meta.get("bundled_images", []):
        images.append({
            "id": None,
            "url": cp.bundled_image_url(name, filename),
            "placeholder": f"images/{filename}",
            "source": "filesystem",
        })

    storage = _image_storage()
    uploaded_rows = (await db.execute(
        select(ModelDocImage).where(ModelDocImage.model_name == name)
    )).scalars().all()
    for row in uploaded_rows:
        try:
            url = await storage.presigned_get_url(
                row.object_key, expires=settings.dashboard_image_url_ttl_seconds,
            )
        except Exception:
            # MinIO unavailable — still surface the image record with empty URL.
            url = ""
        images.append({
            "id": str(row.id),
            "url": url,
            "placeholder": f"dashboard://image/{row.id}",
            "source": "uploaded",
            "original_name": row.original_name,
        })

    # Lifecycle gates: all three stages with their thresholds for the dashboard gauge
    lifecycle_raw: list[dict] = (meta.get("promotion") or {}).get("lifecycle") or []
    lifecycle_gates = [
        {
            "name": gate.get("name"),
            "metric": gate.get("metric"),
            "threshold": gate.get("threshold"),
            "direction": gate.get("direction", "higher_is_better"),
        }
        for gate in lifecycle_raw
        if gate.get("name")
    ]

    # Retraining schedule from Prefect config
    prefect_cfg: dict = meta.get("prefect") or {}
    retraining = {
        "schedule": prefect_cfg.get("schedule"),
        "deployment_name": prefect_cfg.get("deployment_name"),
        "work_pool": prefect_cfg.get("work_pool"),
        "concurrency_limit": prefect_cfg.get("concurrency_limit"),
    }

    return {
        "name": name,
        "task_type": meta["task_type"],
        "frontmatter": {
            "display_name": fm.display_name,
            "summary": fm.summary,
            "paper": fm.paper,
            "use_cases": fm.use_cases,
            "maintainers": fm.maintainers,
            "tags": fm.tags,
            "status": fm.status,
            "last_reviewed": fm.last_reviewed.isoformat() if fm.last_reviewed else None,
        },
        "frontmatter_warnings": warnings,
        "description": {
            "body": body,
            "source": source,
            "upstream_drift": upstream_drift,
            "updated_at": updated_at,
        },
        "technical": {
            "estimator_class": meta["estimator_class"],
            "supported_datasets": meta["supported_datasets"],
            "input_schema": meta["input_schema"],
            "output_schema": meta["output_schema"],
            "promotion": meta["promotion"],
            "hyperparameters": meta.get("hyperparameters") or {},
        },
        "lifecycle_gates": lifecycle_gates,
        "retraining": retraining,
        "seanerbus_uuid": meta.get("seanerbus_uuid"),
        "stages": stages,
        "links": links,
        "images": images,
    }


def _ray_client() -> httpx.AsyncClient:
    """Module-level factory; tests monkeypatch this with a MockTransport."""
    return httpx.AsyncClient(base_url=settings.ray_serve_url, timeout=15.0)


@router.post("/{name}/predict")
async def predict(
    name: str,
    payload: dict = Body(...),
    stage: str | None = None,
    version: str | None = None,
    _claims: dict = Depends(require_role("viewer")),
) -> dict:
    params: dict[str, str] = {}
    if stage:
        params["stage"] = stage
    if version:
        params["version"] = version
    async with _ray_client() as ray:
        try:
            r = await ray.post(f"/predict/{name}", json=payload, params=params)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Ray Serve unavailable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


class DescriptionUpdate(BaseModel):
    markdown: str


@router.put("/{name}/description")
async def put_description(
    name: str,
    payload: DescriptionUpdate,
    claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cp = _control_plane()
    try:
        await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc

    _text, fs_sha = await cp.get_readme(name)
    role = str(claims.get("role", "admin"))

    existing = await db.get(ModelDocOverride, name)
    if existing:
        existing.body = payload.markdown
        existing.fs_sha = fs_sha
        existing.updated_by = role
    else:
        db.add(ModelDocOverride(
            model_name=name, body=payload.markdown, fs_sha=fs_sha, updated_by=role,
        ))
    await db.commit()
    return {"ok": True}


@router.delete("/{name}/description", status_code=status.HTTP_204_NO_CONTENT)
async def delete_description(
    name: str,
    _claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = await db.get(ModelDocOverride, name)
    if row:
        await db.delete(row)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/svg+xml",
}
_MIME_TO_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
}


@router.post("/{name}/images")
async def upload_image(
    name: str,
    file: UploadFile = File(...),
    claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cp = _control_plane()
    try:
        await cp.get_meta(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown model {name!r}") from exc

    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported media type: {file.content_type}")
    data = await file.read()
    if len(data) > settings.dashboard_max_image_bytes:
        raise HTTPException(status_code=413, detail="Image exceeds maximum size")

    storage = _image_storage()
    await storage.ensure_bucket()
    image_id = uuid.uuid4()
    ext = _MIME_TO_EXT[file.content_type]
    key = f"{name}/{image_id}{ext}"
    await storage.put(key=key, data=data, content_type=file.content_type)

    role = str(claims.get("role", "admin"))
    row = ModelDocImage(
        id=image_id,
        model_name=name,
        object_key=key,
        original_name=file.filename or "untitled",
        content_type=file.content_type,
        size_bytes=len(data),
        uploaded_by=role,
    )
    db.add(row)
    await db.commit()

    url = await storage.presigned_get_url(
        key, expires=settings.dashboard_image_url_ttl_seconds,
    )
    return {
        "id": str(image_id),
        "placeholder": f"dashboard://image/{image_id}",
        "url": url,
        "size_bytes": len(data),
    }


@router.delete("/{name}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    name: str,
    image_id: str,
    _claims: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Tolerate both UUID-string and bare strings (sqlite stores as str via .with_variant).
    try:
        lookup_id = uuid.UUID(image_id)
    except ValueError:
        lookup_id = image_id
    row = await db.get(ModelDocImage, lookup_id)
    if row is None or row.model_name != name:
        raise HTTPException(status_code=404, detail="Image not found")
    storage = _image_storage()
    await storage.delete(row.object_key)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{name}/costs")
async def get_model_costs(name: str, _=Depends(require_role("viewer"))) -> list[dict]:
    """HPC cost history for a model from platform.db."""
    import os as _os
    import sqlite3 as _sql
    db_path = _os.getenv("PLATFORM_DB", "/repo/platform.db")
    try:
        conn = _sql.connect(db_path)
        conn.row_factory = _sql.Row
        rows = conn.execute(
            "SELECT version, run_id, job_id, gpu_hours, cost_usd, recorded_at "
            "FROM model_costs WHERE model_name=? ORDER BY version ASC, id ASC",
            (name.upper(),),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT version, run_id, job_id, gpu_hours, cost_usd, recorded_at "
                "FROM model_costs WHERE model_name=? ORDER BY version ASC, id ASC",
                (name.lower(),),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
