from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from skipper import config

_HOWTO = {
    "add a model": (
        "guides/add-a-new-model.md",
        "exa scaffold DemoAD --task anomaly_detection --type classification",
        "Edit the generated pipelines/models/<name>.yaml + transforms shim, then commit.",
    ),
    "deploy a pipeline": (
        "guides/quickstart.md",
        "exa pipeline deploy --registry pipelines/model_registry.yaml --env prod",
        "Deploys one Prefect flow per model from the YAML registry.",
    ),
    "promote a version": (
        "components/mlflow.md",
        "(automatic) promote_task walks the lifecycle rules in get_inference_params().",
        "Set lifecycle thresholds in pipelines/models/<name>.yaml.",
    ),
    "trigger a retrain": (
        "guides/control-plane.md",
        "exa retrain JPCP --dataset PM100Dataset --dummy",
        "Or use the trigger_retrain tool (asks for confirmation).",
    ),
    "add a dataset backend": (
        "components/modelzoo.md",
        "Pass --backend minio|dataplane to exa pipeline run.",
        "Backends live in modelzoo/modelzoo/datasets/_backends.py.",
    ),
}


def _docs_root() -> Path:
    return Path(config.AGENT_DOCS_ROOT).resolve()


@tool
def list_docs() -> str:
    """List the available documentation files (relative paths under the docs root)."""
    root = _docs_root()
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.md"))
    return "\n".join(files) if files else "No docs found."


@tool
def search_docs(query: str) -> str:
    """Search the documentation for a term; returns matching file:line snippets.

    Args:
        query: Text to search for (case-insensitive).
    """
    root = _docs_root()
    rg = shutil.which("rg")
    if rg:
        proc = subprocess.run(  # noqa: S603
            [rg, "-i", "-n", "--max-count", "3", query, str(root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = proc.stdout.strip()
        return out[:4000] if out else f"No matches for {query!r}."
    hits = []
    for path in root.rglob("*.md"):
        for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if query.lower() in line.lower():
                hits.append(f"{path.relative_to(root)}:{i}: {line.strip()}")
                if len(hits) >= 30:
                    break
    return "\n".join(hits) if hits else f"No matches for {query!r}."


@tool
def read_doc(path: str) -> str:
    """Read a documentation file by its relative path (under the docs root or CLAUDE.md).

    Args:
        path: Relative path, e.g. 'guides/agent.md'.
    """
    root = _docs_root()
    if Path(path).name == "CLAUDE.md":
        return Path(config.CLAUDE_MD).read_text(errors="ignore")[:8000]
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return "Error: path is outside the docs root."
    if not target.exists():
        return f"Error: {path} not found."
    return target.read_text(errors="ignore")[:8000]


@tool
def get_howto(topic: str) -> str:
    """Get a how-to / best-practice for a common task: the guide, the command, and a tip.

    Args:
        topic: One of: 'add a model', 'deploy a pipeline', 'promote a version',
            'trigger a retrain', 'add a dataset backend'.
    """
    key = topic.strip().lower()
    if key not in _HOWTO:
        return "Unknown topic. Known: " + ", ".join(_HOWTO) + ". Use search_docs for anything else."
    guide, command, tip = _HOWTO[key]
    return f"How to {key}:\n  guide: {guide}\n  command: {command}\n  tip: {tip}"


TOOLS = [list_docs, search_docs, read_doc, get_howto]
