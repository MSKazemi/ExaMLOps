"""A default endpoint may not name a container-internal port on ``localhost``.

Every service in this stack is published on the host under the project's +10000 offset —
``14200:4200`` for Prefect, ``19000:9000`` for MinIO, ``18099:8099`` for the dashboard. The two
addresses that are ever correct for a client are therefore the *host* port (``localhost:14200``)
and the *service name* inside the compose network (``http://orchestrator:4200/api``, which
compose sets itself). ``localhost:4200`` is neither: on the host nothing listens there, and inside
the network ``localhost`` is the caller's own container.

That default is silent when it is wrong. The control plane reported a perfectly healthy Prefect as
down on ``/status`` and posted its retrain flow runs into a closed port, for as long as it ran
outside compose. `exa backup create` reached ``localhost:9000`` for MinIO the same way. Neither
raised; both produced a plausible answer that was about nothing.

The rule is derived, not listed: the compose files are the source of truth for which ports are
container-internal, so publishing a new service brings its port under the guard automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("platform", "pipelines", "serving", "usecases")

# (path, port) → why naming the container port on localhost is correct *there*.
ALLOWED: dict[tuple[str, int], str] = {
    ("serving/inference_pipeline/app.py", 8001): (
        "this module is itself a Ray Serve deployment: it runs inside the ray-serving container, "
        "where 8001 is Ray Serve's own HTTP port on its own loopback."
    ),
    ("platform/services/dashboard/backend/settings.py", 8003): (
        "the SeanerBUS bridge is also run bare-metal in dev (`run_status_server(port=8003)`), "
        "where it serves on the host's 8003 with no port mapping at all."
    ),
    ("platform/services/dashboard/backend/routers/seanerbus.py", 8003): (
        "same bare-metal bridge as settings.py — the container path is set explicitly by compose."
    ),
}


def _published() -> dict[int, int]:
    """container port → host port, for every mapping the compose files publish."""
    pairs: dict[int, int] = {}
    for compose in sorted(REPO.glob("**/docker-compose*.yml")):
        if ".venv" in compose.parts or "node_modules" in compose.parts:
            continue
        for host, container in re.findall(r'"(\d{2,5}):(\d{2,5})"', compose.read_text()):
            if host != container:
                pairs.setdefault(int(container), int(host))
    return pairs


def _sources() -> list[Path]:
    out = []
    for d in SOURCE_DIRS:
        for path in (REPO / d).rglob("*.py"):
            parts = set(path.parts)
            if parts & {".venv", "node_modules", "build", "__pycache__", "tests"}:
                continue
            out.append(path)
    return out


def test_the_compose_files_still_publish_the_ports_this_guard_reads():
    """If the parse ever comes back empty the guard passes by vacuum — say so instead."""
    published = _published()
    assert len(published) >= 10, f"only parsed {len(published)} mappings; the compose format moved"
    assert published[4200] == 14200
    assert published[9000] == 19000


def test_no_source_default_points_at_a_container_port_on_localhost():
    published = _published()
    pattern = re.compile(r"localhost:(\d{2,5})")
    offenders = []
    for path in _sources():
        rel = path.relative_to(REPO).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for port in (int(p) for p in pattern.findall(line)):
                if port not in published or (rel, port) in ALLOWED:
                    continue
                offenders.append(
                    f"{rel}:{lineno} names localhost:{port}, which is the *container* port; "
                    f"on this host the stack publishes it as {published[port]}, and inside the "
                    f"compose network the address is the service name, not localhost"
                )
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(("rel", "port"), sorted(ALLOWED))
def test_every_exemption_still_describes_something_real(rel, port):
    """An exemption that outlives its line is a hole nobody can see."""
    path = REPO / rel
    assert path.exists(), f"{rel} is exempted from the localhost-port guard but no longer exists"
    assert f"localhost:{port}" in path.read_text(), (
        f"{rel} no longer names localhost:{port} — drop the exemption rather than leaving it open"
    )
