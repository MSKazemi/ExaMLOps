import os

import docker
from docker import DockerClient
from docker.errors import DockerException

_client: DockerClient | None = None
_project: str | None = None
_project_detected = False


def get_docker_client() -> DockerClient | None:
    global _client
    if _client is not None:
        return _client
    try:
        c = docker.from_env()
        c.ping()
        _client = c
    except DockerException:
        pass
    return _client


def get_own_project() -> str | None:
    """Read the compose project name from this container's own labels."""
    global _project, _project_detected
    if _project_detected:
        return _project
    _project_detected = True
    client = get_docker_client()
    if client is None:
        return None
    hostname = os.environ.get("HOSTNAME", "")
    try:
        own = client.containers.get(hostname)
        _project = own.labels.get("com.docker.compose.project")
    except Exception:
        pass
    return _project
