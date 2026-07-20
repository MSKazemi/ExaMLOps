import asyncio
import datetime
import os
import threading
from typing import Any

from auth import require_role
from docker_client import get_docker_client, get_own_project
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["containers"])

_viewer_dep = require_role("viewer")
_admin_dep = require_role("admin")

DISPLAY_NAMES: dict[str, str] = {
    "ray-serving": "Ray Serve",
    "mlflow": "MLflow",
    "orchestrator": "Prefect",
    "control-plane": "Control Plane",
    "minio": "MinIO",
    "minio-init": "MinIO Init",
    "postgres": "PostgreSQL",
    "prometheus": "Prometheus",
    "grafana": "Grafana",
    "loki": "Loki",
    "promtail": "Promtail",
    "seanerbus-sim": "SeanerBUS",
    "seanerbus-bridge": "SeanerBUS Bridge",
    "dashboard": "Dashboard",
}


# One-shot init containers — run once then exit; not useful to expose in the UI.
_INIT_CONTAINERS: set[str] = {"minio-init"}


def _require_docker():
    client = get_docker_client()
    project = get_own_project()
    if client is None or project is None:
        raise HTTPException(503, "Docker unavailable")
    return client, project


def _find_container(client, project: str, service: str):
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"com.docker.compose.project={project}",
                f"com.docker.compose.service={service}",
            ]
        },
    )
    if not containers:
        raise HTTPException(404, f"Container '{service}' not found")
    return containers[0]


def _uptime(container) -> str:
    if container.status != "running":
        return ""
    started = container.attrs.get("State", {}).get("StartedAt", "")
    if not started:
        return ""
    try:
        t = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
        if t.year < 2000:  # Docker sentinel: 0001-01-01T00:00:00Z
            return ""
        delta = datetime.datetime.now(datetime.UTC) - t
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except ValueError:
        return ""


def _health(container) -> str:
    h = container.attrs.get("State", {}).get("Health", {})
    return h.get("Status", "none") if h else "none"


def _image_ref(c) -> str:
    """Image name for a container without touching the ``/images`` API.

    ``c.image`` triggers an image-inspect round-trip, which a hardened
    docker-socket-proxy (IMAGES endpoint disabled) rejects with 403 → a 500 for
    the whole request. The container-list/inspect payload already carries the
    image reference (``Config.Image`` on inspect, ``Image`` on the list summary),
    so read it from ``attrs`` instead. Falls back to the short image id.
    """
    attrs = getattr(c, "attrs", {}) or {}
    ref = (attrs.get("Config", {}) or {}).get("Image") or attrs.get("Image") or ""
    if ref and not ref.startswith("sha256:"):
        return ref
    image_id = attrs.get("ImageID") or ref
    return image_id.split(":")[-1][:12] if image_id else "unknown"


def _container_info(c) -> dict[str, Any]:
    service = c.labels.get("com.docker.compose.service", c.name)
    return {
        "name": service,
        "display_name": DISPLAY_NAMES.get(service, service.replace("-", " ").title()),
        "status": c.status,
        "health": _health(c),
        "uptime": _uptime(c),
        "image": _image_ref(c),
    }


@router.get("/containers")
async def list_containers(_user=Depends(_viewer_dep)):
    client, project = _require_docker()

    def _fetch() -> list[dict]:
        # Runs in a worker thread — the Docker SDK is blocking, so keeping it on
        # the event loop would freeze every other dashboard request on a slow or
        # hung Docker socket.
        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={project}"},
        )
        return [
            _container_info(c)
            for c in containers
            if c.labels.get("com.docker.compose.service") not in _INIT_CONTAINERS
        ]

    loop = asyncio.get_running_loop()
    return {"containers": await loop.run_in_executor(None, _fetch)}


@router.post("/containers/{service}/start")
async def start_container(service: str, _user=Depends(_admin_dep)):
    client, project = _require_docker()
    c = _find_container(client, project, service)
    if c.status == "running":
        raise HTTPException(409, "Container already running")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, c.start)
    await loop.run_in_executor(None, c.reload)
    return _container_info(c)


@router.post("/containers/{service}/stop")
async def stop_container(service: str, _user=Depends(_admin_dep)):
    client, project = _require_docker()
    c = _find_container(client, project, service)
    if c.status not in ("running", "restarting"):
        raise HTTPException(409, "Container not running")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, c.stop)
    await loop.run_in_executor(None, c.reload)
    return _container_info(c)


async def _restart_after_delay(container, delay: float) -> None:
    await asyncio.sleep(delay)
    container.restart()


@router.post("/containers/{service}/restart")
async def restart_container(
    service: str,
    background_tasks: BackgroundTasks,
    _user=Depends(_admin_dep),
):
    client, project = _require_docker()
    c = _find_container(client, project, service)

    # Detect if we are restarting the dashboard container itself
    hostname = os.environ.get("HOSTNAME", "")
    is_self = False
    try:
        own_c = client.containers.get(hostname)
        is_self = own_c.labels.get("com.docker.compose.service", "") == service
    except Exception:
        pass

    if is_self:
        background_tasks.add_task(_restart_after_delay, c, 2.0)
        return {"status": "restarting", "reconnect_after_ms": 4000}

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, c.restart)
    await loop.run_in_executor(None, c.reload)
    return _container_info(c)


@router.get("/containers/{service}/logs")
async def get_logs(service: str, lines: int = 100, _user=Depends(_viewer_dep)):
    client, project = _require_docker()
    c = _find_container(client, project, service)
    raw = c.logs(tail=lines, timestamps=True)
    return {"logs": raw.decode("utf-8", errors="replace")}


@router.get("/containers/{service}/logs/stream")
async def stream_logs(service: str, _user=Depends(_viewer_dep)):
    client, project = _require_docker()
    c = _find_container(client, project, service)

    async def _generate():
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _worker():
            try:
                for chunk in c.logs(stream=True, follow=True, timestamps=True):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception:
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_worker, daemon=True).start()

        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            line = chunk.decode("utf-8", errors="replace").rstrip().replace("\n", " ")
            yield f"data: {line}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")
