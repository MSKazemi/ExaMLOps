"""Shared fixtures for the agent-runtime tests (ADR 0144/0145/0146). Not a test module."""

from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.agent_runtime import (  # noqa: E402
    AgentProgram,
    AgentRuntime,
    AgentStateStore,
    Node,
)
from examlops.agent_runtime.snapshot import SCHEMA_VERSION, digest_of  # noqa: E402
from examlops.agent_versions.manifest import normalize, version_id_of  # noqa: E402
from examlops.mcp.tools import ToolSpec  # noqa: E402

DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/jobdoc@" + DIGEST


def manifest(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "agent": "jobdoc",
        "code": {"image": IMAGE, "entrypoint": "tests.jobdoc:program", "framework": "python"},
        "prompts": [{"name": "jobdoc-system", "version": 7}],
        "models": [
            {
                "role": "planner",
                "servable": "gen://qwen3-32b",
                "binding": "follow",
                "alias": "Production",
            },
            {"role": "embedder", "servable": "gen://embed-e5", "binding": "pin", "version": 4},
        ],
        "tools": {"tools": [], "grants": []},
        "policy": {"contract": "jobdoc-v2", "autonomy": "L2", "multitask_strategy": "enqueue"},
    }
    for k, v in over.items():
        if k == "policy":
            doc["policy"] = {**doc["policy"], **v}
        else:
            doc[k] = v
    return normalize(doc)


def snapshot(
    versions: list[dict[str, Any]],
    *,
    aliases: dict[str, str] | None = None,
    canary_percent: float = 0.0,
    migrations: dict[str, Any] | None = None,
    retired: dict[str, str] | None = None,
    grants: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
    pins: dict[str, Any] | None = None,
    quotas: dict[str, Any] | None = None,
    generation: int = 1,
) -> dict[str, Any]:
    vmap = {version_id_of(m): m for m in versions}
    first = next(iter(vmap))
    doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generation": generation,
        "compiled_at": "2026-09-25T00:00:00Z",
        "agents": {
            "jobdoc": {
                "aliases": aliases or {"Production": first},
                "canary_percent": canary_percent,
                "migrations": migrations or {},
                "retired": retired or {},
            }
        },
        "versions": vmap,
        "grants": grants or {},
        "models": models if models is not None else {"qwen3-32b": {"Production": "7"}},
        "reeval_pins": pins or {},
        "quotas": quotas or {"default": {"max_sessions": 100}, "tenants": {}},
    }
    doc["digest"] = digest_of(doc)
    return doc


class Tools:
    """A tiny tool registry whose calls are counted - the side effects under test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()

        def submit_job(name: str = "job") -> dict[str, Any]:
            """Submit a job (a side effect)."""
            with self.lock:
                self.calls.append(("submit_job", {"name": name}))
            return {"ok": True, "job_id": f"job-{len(self.calls)}"}

        def search_docs(q: str = "") -> dict[str, Any]:
            """Search the docs (a read)."""
            with self.lock:
                self.calls.append(("search_docs", {"q": q}))
            return {"ok": True, "hits": [q.upper()]}

        self.specs = {
            "submit_job": ToolSpec(fn=submit_job, mutating=True, tier="B"),
            "search_docs": ToolSpec(fn=search_docs),
        }

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class Crash(BaseException):
    """A worker dying mid-node (not an Exception: the runtime must not catch it)."""


def make_runtime(
    tmp_path: Path, snap: dict[str, Any], programs: dict[str, Any], **kw: Any
) -> AgentRuntime:
    store = kw.pop("store", None) or AgentStateStore(str(tmp_path / "agent_state.db"))

    def loader(m: dict[str, Any]) -> Any:
        return programs[m["code"]["entrypoint"]]

    return AgentRuntime(store, snapshot=copy.deepcopy(snap), program_loader=loader, **kw)


def linear_program(*nodes: Node, schema: dict[str, Any] | None = None) -> AgentProgram:
    return AgentProgram(list(nodes), state_schema=schema)
