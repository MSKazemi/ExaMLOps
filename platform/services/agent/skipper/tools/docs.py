from __future__ import annotations

import re
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
        "Backends live in modelzoo/seanergys_modelzoo/datasets/_backends.py.",
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


#: Words that carry no retrieval signal in a question like "can I use an LLM judge to gate
#: promotion?" — dropped before term matching so the remaining words decide the ranking.
_STOP_WORDS = frozenset(
    """a about an and any are as at be by can could do does for from get give has have how i if in
    is it its me my need of on or please should show some tell that the their there these they this
    to use used using want was what when where which who why will with would you your""".split()
)


def _terms(query: str) -> list[str]:
    """The words in ``query`` worth searching for, longest first (most specific wins ties)."""
    # Split on every non-word character, so a hyphenated compound ("LLM-as-a-judge") and a file
    # name ("judge-calibration.md") both break into the words that actually retrieve.
    words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    kept = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]
    return sorted(dict.fromkeys(kept), key=len, reverse=True)


def _phrase_hits(root: Path, query: str) -> str:
    """The original literal search: exact phrase, `rg` when present, Python otherwise."""
    rg = shutil.which("rg")
    if rg:
        proc = subprocess.run(  # noqa: S603
            [rg, "-i", "-n", "--max-count", "3", "-F", query, str(root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip()[:4000]
    hits = []
    needle = query.lower()
    for path in sorted(root.rglob("*.md")):
        for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{path.relative_to(root)}:{i}: {line.strip()}")
                if len(hits) >= 30:
                    return "\n".join(hits)
    return "\n".join(hits)


def _term_hits(root: Path, terms: list[str], limit: int = 8) -> str:
    """Rank files by how many of ``terms`` they contain, then quote one line per matched term.

    This is what makes a natural-language question findable. Ranking by *distinct terms matched*
    (not by raw count) keeps a long file from crowding out the one that is actually about the
    subject, and the tie-break on total occurrences favours a guide over a passing mention.
    """
    scored: list[tuple[int, int, str, list[str]]] = []
    for path in sorted(root.rglob("*.md")):
        text = path.read_text(errors="ignore")
        lowered = text.lower()
        matched = [term for term in terms if term in lowered]
        if not matched:
            continue
        total = sum(lowered.count(term) for term in matched)
        lines = text.splitlines()
        quotes = []
        for term in matched[:3]:
            for i, line in enumerate(lines, 1):
                if term in line.lower():
                    quotes.append(f"  {path.relative_to(root)}:{i}: {line.strip()[:160]}")
                    break
        scored.append((len(matched), total, str(path.relative_to(root)), quotes))
    if not scored:
        return ""
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    out = []
    for n_matched, _total, rel, quotes in scored[:limit]:
        out.append(f"{rel}  [matched {n_matched}/{len(terms)} terms]")
        out.extend(quotes)
    return "\n".join(out)[:4000]


@tool
def search_docs(query: str) -> str:
    """Search the documentation for a term or a question; returns matching file:line snippets.

    Args:
        query: Text to search for (case-insensitive). A natural-language question works —
            when the exact phrase is not present, the words are matched individually and the
            files are ranked by how many of them they contain.
    """
    root = _docs_root()
    phrase = _phrase_hits(root, query)
    if phrase:
        return phrase

    # The phrase is absent — which is the normal case for a question, and must not be reported as
    # "not documented". Fall back to the query's words before concluding anything.
    terms = _terms(query)
    if terms:
        ranked = _term_hits(root, terms)
        if ranked:
            return (
                f"No file contains the exact phrase {query!r}; ranking by its terms "
                f"({', '.join(terms)}) instead:\n{ranked}"
            )

    # Genuinely nothing. Say what was tried, so an empty result is not mistaken for an absent
    # capability — the failure mode this fallback exists to prevent.
    tried = f"the phrase, then its terms ({', '.join(terms)})" if terms else "the phrase"
    return (
        f"No documentation matched {query!r} — tried {tried}. This means the search found "
        f"nothing, NOT that the platform lacks the capability. Use list_docs to see what exists, "
        f"or search a single distinctive word."
    )


@tool
def read_doc(path: str) -> str:
    """Read a public documentation file by its relative path under the docs root.

    Args:
        path: Relative path, e.g. 'guides/agent.md'.
    """
    root = _docs_root()
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
