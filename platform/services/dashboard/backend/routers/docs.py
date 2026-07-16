"""Documentation router — serves the full public docs set to the dashboard Docs page.

The section tree is built from the curated ``mkdocs.yml`` nav (so the dashboard mirrors the
published documentation site's structure, titles, and order). Any markdown file under ``docs/``
that is not yet referenced by the nav is auto-discovered and appended under an *Additional*
section, so no public document is ever silently missing. Everything is viewer-gated, and
``/content`` only serves text files resolved safely inside the project root.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from auth import require_role
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/docs", dependencies=[Depends(require_role("viewer"))])

# Top-level docs/ subdirectories, mapped to friendly section titles for the auto-discovery pass.
_SUBDIR_TITLES = {
    "guides": "Guides",
    "tutorials": "Tutorials",
    "components": "Components",
    "dashboard": "Dashboard",
    "reference": "Reference",
}
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")


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


try:
    _ROOT: Path | None = _find_root()
except RuntimeError:
    _ROOT = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _pretty(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ").strip().title()


def _title_from_file(rel_path: str) -> str:
    """Human title for a doc: its first H1 heading, else a prettified filename."""
    if _ROOT is not None:
        abs_path = _ROOT / rel_path
        try:
            with abs_path.open(encoding="utf-8") as fh:
                for _ in range(60):  # H1 is near the top of well-formed docs
                    line = fh.readline()
                    if not line:
                        break
                    m = _H1_RE.match(line)
                    if m:
                        return m.group(1)
        except OSError:
            pass
    return _pretty(Path(rel_path).stem)


def _walk_nav(items: Any, crumb: str, out: list[tuple[str, str, str]]) -> None:
    """Flatten an mkdocs nav list into (section_title, file_title, docs_relative_path) rows.

    Nested nav categories become breadcrumb section titles ("Parent · Child"), preserving the
    curated order. Both ``{Title: value}`` and bare-string nav entries are handled.
    """
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, str):  # bare "guides/foo.md"
            rel = f"docs/{item}"
            out.append((crumb or "Docs", _title_from_file(rel), rel))
        elif isinstance(item, dict):
            for title, val in item.items():
                if isinstance(val, str):  # leaf: "Title: guides/foo.md"
                    rel = f"docs/{val}"
                    out.append((crumb or "Docs", title, rel))
                elif isinstance(val, list):  # category: recurse with extended breadcrumb
                    child = f"{crumb} · {title}" if crumb else title
                    _walk_nav(val, child, out)


def _nav_rows() -> list[tuple[str, str, str]]:
    """Rows from mkdocs.yml nav; empty if the file is absent or unparseable."""
    if _ROOT is None:
        return []
    mkdocs = _ROOT / "mkdocs.yml"
    if not mkdocs.exists():
        return []
    try:
        # mkdocs uses !!python/name: tags; the plain loader would choke, so ignore unknown tags.
        loader = yaml.SafeLoader
        loader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda *_: None)
        loader.add_multi_constructor("!!python/name:", lambda *_: None)
        data = yaml.load(mkdocs.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506
    except (OSError, yaml.YAMLError):
        return []
    rows: list[tuple[str, str, str]] = []
    _walk_nav((data or {}).get("nav", []), "", rows)
    return rows


def _discover_rows(known: set[str]) -> list[tuple[str, str, str]]:
    """Every docs/**/*.md not already in ``known``, grouped by top-level subdirectory."""
    if _ROOT is None:
        return []
    rows: list[tuple[str, str, str]] = []
    for md in sorted((_ROOT / "docs").rglob("*.md")):
        rel = md.relative_to(_ROOT).as_posix()
        if rel in known:
            continue
        parts = md.relative_to(_ROOT / "docs").parts
        subdir = parts[0] if len(parts) > 1 else "docs"
        section = _SUBDIR_TITLES.get(subdir, _pretty(subdir))
        rows.append((f"Additional · {section}", _title_from_file(rel), rel))
    return rows


@lru_cache(maxsize=1)
def _sections() -> list[dict]:
    """Build the ordered section tree once: Overview → mkdocs nav → auto-discovered extras."""
    if _ROOT is None:
        return []

    sections: list[dict] = []
    index: dict[str, dict] = {}
    seen_paths: set[str] = set()

    def add(section_title: str, file_title: str, rel: str) -> None:
        if rel in seen_paths or not (_ROOT / rel).exists():
            return
        seen_paths.add(rel)
        sec = index.get(section_title)
        if sec is None:
            sec = {"key": _slug(section_title), "title": section_title, "files": []}
            index[section_title] = sec
            sections.append(sec)
        sec["files"].append({"path": rel, "title": file_title})

    # 1) Project overview (README) always first.
    add("Overview", "Project Overview", "README.md")
    # 2) Curated mkdocs nav (structure + titles + order).
    for section_title, file_title, rel in _nav_rows():
        add(section_title, file_title, rel)
    # 3) Auto-discovered docs not covered by the nav — nothing public goes missing.
    for section_title, file_title, rel in _discover_rows(set(seen_paths)):
        add(section_title, file_title, rel)

    return [s for s in sections if s["files"]]


def _safe_resolve(rel_path: str) -> Path:
    if _ROOT is None:
        raise HTTPException(
            status_code=503, detail="Docs root not configured. Set EXAMLOPS_DOCS_ROOT."
        )
    root_str = str(_ROOT)
    resolved = (_ROOT / rel_path).resolve()
    if not (str(resolved) == root_str or str(resolved).startswith(root_str + "/")):
        raise HTTPException(status_code=400, detail="Invalid path")
    return resolved


@router.get("/tree")
async def get_docs_tree() -> list[dict]:
    return _sections()


@router.get("/content", response_class=PlainTextResponse)
async def get_doc_content(path: str = Query(..., min_length=1, max_length=500)) -> str:
    resolved = _safe_resolve(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if resolved.suffix.lower() not in {".md", ".txt", ".rst"}:
        raise HTTPException(status_code=400, detail="Only text files are served")
    return resolved.read_text(encoding="utf-8")
