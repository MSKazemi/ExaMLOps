"""README frontmatter parser.

Returns a typed Frontmatter, the markdown body, and a list of human-readable
warnings. Never raises on malformed input — always falls back gracefully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_VALID_STATUS = {"stable", "experimental", "deprecated"}


@dataclass
class Frontmatter:
    display_name: str | None = None
    summary: str | None = None
    paper: dict[str, str] = field(default_factory=dict)
    use_cases: list[str] = field(default_factory=list)
    maintainers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    status: str | None = None
    last_reviewed: date | None = None


def parse_readme(text: str) -> tuple[Frontmatter, str, list[str]]:
    match = _FM_RE.match(text)
    if not match:
        return Frontmatter(), text, []

    yaml_text, body = match.group(1), match.group(2)
    warnings: list[str] = []
    try:
        data: Any = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        warnings.append(f"YAML parse error: {exc}")
        return Frontmatter(), body, warnings

    if not isinstance(data, dict):
        warnings.append("Frontmatter root must be a mapping")
        return Frontmatter(), body, warnings

    fm = Frontmatter()
    fm.display_name = _coerce_str(data.get("display_name"))
    fm.summary = _coerce_str(data.get("summary"))
    paper = data.get("paper")
    if isinstance(paper, dict):
        fm.paper = {k: str(v) for k, v in paper.items() if isinstance(k, str)}
    fm.use_cases = _coerce_str_list(data.get("use_cases"))
    fm.maintainers = _coerce_str_list(data.get("maintainers"))
    fm.tags = _coerce_str_list(data.get("tags"))

    status = data.get("status")
    if status is None:
        pass
    elif isinstance(status, str) and status in _VALID_STATUS:
        fm.status = status
    else:
        warnings.append(f"status must be one of {sorted(_VALID_STATUS)}, got {status!r}")

    lr = data.get("last_reviewed")
    if lr is None:
        pass
    elif isinstance(lr, date):
        fm.last_reviewed = lr
    elif isinstance(lr, str):
        try:
            fm.last_reviewed = date.fromisoformat(lr)
        except ValueError:
            warnings.append(f"last_reviewed must be ISO YYYY-MM-DD, got {lr!r}")
    else:
        warnings.append(f"last_reviewed must be a date, got {type(lr).__name__}")

    return fm, body, warnings


def _coerce_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]
