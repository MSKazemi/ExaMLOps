"""Platform callers use the control plane's /v1 paths, not the deprecated ones (plan P1.6).

Every deprecated route whose /v1 twin is the *same handler* can be migrated by changing the path
alone — nothing about the request or the answer differs except that errors arrive as RFC 9457
problem documents, which keep the ``detail`` string callers read. So no platform caller (CLI, MCP
tools, SDK, dashboard, agent, clients, pipelines, serving) may still build one: the paths are
announced as deprecated to every outside client, and the platform should not be its own last user.

``POST /retrain`` is not a same-handler twin: ``/v1/retrain`` is the asynchronous command API (202
and a command to follow). Its eleven platform callers moved to ``examlops.retrain_command``, which
submits there and waits for the dispatch (plan P1.6c), so it is forbidden too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "services" / "control_plane"))

from cplane.versioning import ALIASES  # noqa: E402

SCANNED = [
    ROOT / "platform" / "cli" / "src",
    ROOT / "platform" / "services" / "dashboard" / "backend",
    ROOT / "platform" / "services" / "agent" / "skipper",
    ROOT / "platform" / "clients",
    ROOT / "pipelines",
    ROOT / "serving",
]

# A path appended to a control-plane base: f"{cfg.control_plane_url}/x", f"{CONTROL_PLANE_URL}/x",
# f"{settings.control_plane_url}/x", f"{load_config().control_plane_url.rstrip('/')}/x".
_AFTER_BASE = re.compile(
    r"(?:control_plane_url|CONTROL_PLANE_URL)(?:\.rstrip\([^)]*\))?\}(?P<path>/[^\"'\s?]*)"
)
# The dashboard's clients take a path relative to the base they were built with.
_RELATIVE = re.compile(r"(?:self\._get\(|_control_plane\(\s*\"[A-Z]+\",\s*)f?\"(?P<path>/[^\"]*)\"")


def _shape(path: str) -> str:
    """``/models/{name}/meta`` and ``/models/{anything}/meta`` compare equal."""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("/"))


def _sources():
    for base in SCANNED:
        for path in scan_files(base):
            parts = set(path.parts)
            if "tests" in parts or ".venv" in parts or "node_modules" in parts:
                continue
            yield path


def _call_sites() -> list[tuple[str, int, str]]:
    sites = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (_AFTER_BASE, _RELATIVE):
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                sites.append((str(path.relative_to(ROOT)), line, m.group("path")))
    return sites


def test_the_scan_sees_the_callers():
    """Guard the guard: a pattern that stopped matching would pass everything below."""
    shapes = {_shape(p) for _, _, p in _call_sites()}
    for expected in ("/v1/approvals", "/v1/models/{}/meta", "/v1/modelzoo/status", "/v1/retrain"):
        assert expected in shapes, f"the scan no longer finds {expected}"
    assert len(_call_sites()) >= 30


def test_no_caller_builds_a_deprecated_path():
    deprecated = {_shape(a.legacy): a.v1 for a in ALIASES}
    offenders = [
        f"{where}:{line}  {path}  → use {deprecated[_shape(path)]}"
        for where, line, path in _call_sites()
        if _shape(path) in deprecated
    ]
    assert not offenders, "callers of deprecated control-plane paths:\n" + "\n".join(offenders)
