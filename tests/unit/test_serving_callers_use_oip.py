"""Platform code calls the model server over Open Inference Protocol v2, not /predict (ADR 0126).

``/predict/{model}`` is deprecated: it answers with ``Deprecation`` and a ``Link`` to
``/v2/models/{model}/infer``. The platform's own callers (the inference router, the bus bridge, the
agent, the dashboard, ``exa batch``, the example client) moved to OIP v2 through
``examlops.oip_client``, and moving them found two callers that had never worked: the agent sent a
feature *list* that /predict refused, and the dashboard sent its stage and version as query
parameters /predict never read. This guard keeps a new caller from reaching for /predict again.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [
    ROOT / "platform" / "cli" / "src",
    ROOT / "platform" / "services",
    ROOT / "platform" / "clients",
    ROOT / "pipelines",
    ROOT / "serving",
]
# The route itself, and the pipeline's own deprecation note.
ALLOWED = {ROOT / "serving" / "ray_serving" / "app.py"}
# A URL being built: the path right after a quote, an f-string brace or a URL variable.
# (A single backtick is a TypeScript template string; rST's ``double`` backticks are prose.)
_BUILT = re.compile(r"""(?:["']|(?<!`)`|\}|_URL|_url)\s*/predict/""")


def _sources():
    for base in SCANNED:
        for path in scan_files(base, "*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or path in ALLOWED:
                continue
            parts = set(path.parts)
            if {"tests", "node_modules", ".venv", "build", "dist"} & parts or ".test." in path.name:
                continue
            yield path


def test_no_platform_caller_builds_a_predict_url():
    offenders = []
    for path in _sources():
        for n, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if _BUILT.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, (
        "callers of the deprecated /predict (use examlops.oip_client):\n" + "\n".join(offenders)
    )


def test_the_scan_would_see_one():
    """Guard the guard against a pattern that stopped matching."""
    for line in (
        'f"{RAY_SERVE_URL}/predict/{m}"',
        "'/predict/' + name",
        'ray.post(f"/predict/{n}")',
    ):
        assert _BUILT.search(line), line
    assert not _BUILT.search("``POST /predict/{model}`` is deprecated")
    assert not _BUILT.search("from ``/v2/models/{name}/…`` or ``/predict/{name}``")
    assert _BUILT.search("fetch(`/predict/${name}`)")
