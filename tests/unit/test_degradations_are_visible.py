# tests/unit/test_degradations_are_visible.py
"""A documented degradation must be distinguishable from the normal case.

Three separate defects in one week had the same shape, and none of them was a crash:

- the Governance console recomputed its own audit chain and reported `verified: True` *by
  construction*, so a digest nobody could reproduce looked like tamper-evidence;
- two signing paths caught every exception and returned the same empty signature that means "this
  site configured no signing key", so a broken signer looked like a deliberate policy;
- every provider resolver fell back to the platform's built-in default without a word, so a site's
  own promotion gate, placement score or carbon coefficient could be silently replaced.

In each case the *fallback* was right — a broken plugin must not block a promotion, a serving
replica must keep serving. What was wrong is that the fallback was **indistinguishable from the
ordinary case**, so nothing in the system, ever, said which one had happened.

This guard holds the line. A blanket `except Exception` inside a function whose docstring promises a
degradation must do one of:

- **say something** — log, warn, or return the cause to its caller; or
- **carry its reason** on the `except` line as `# noqa: BLE001 - <why>`, which is the repo's existing
  convention for a deliberate broad catch.

It is a **ratchet**: `BASELINE` is what was left when it was written, and the number may only go
down. That is deliberate — the remaining sites need reading one at a time, and a guard that demanded
them all at once would have been switched off instead. The four left are low-consequence and each
needs a judgement rather than a sweep: a model-zoo adoption retry, a semantic-cache embedding (a
cache miss is a correct answer), the dashboard's provider-metadata badge, and its own UI telemetry.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("platform/cli/src/examlops", "platform/services", "serving", "pipelines")

#: A docstring that promises the function keeps working when something under it fails.
_PROMISE = re.compile(r"degrade[sd]?\b|falls? back\b|rather than failing|instead of failing", re.I)

#: Calls that make a failure visible: logging, a CLI warning, or recording it somewhere.
_VISIBLE = re.compile(
    r"^_?(warning|warn|error|exception|critical|info|audit|record|degraded)", re.I
)

#: How many silent sites remained when this guard was written. It may only go **down**.
BASELINE = 4


def _is_visible(handler: ast.ExceptHandler) -> bool:
    """Does this handler tell anyone? A logged call, or the exception in what it returns."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if _VISIBLE.match(name or ""):
                return True
        # `return {... f"{exc}" ...}` / `raise` — the caller is told either way
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Name) and handler.name and node.id == handler.name:
            return True
    return False


def _silent_sites() -> list[str]:
    out: list[str] = []
    for root in ROOTS:
        for path in scan_files(ROOT / root):
            if "/tests/" in path.as_posix():
                continue
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                # NOT skipped. A file this scan cannot parse is a file it reports **zero** sites
                # for, and the ratchet then reads that as an improvement. Found by mutating a
                # module into invalid Python: the guard went green on a file it could not read.
                raise AssertionError(f"{rel} does not parse, so it was not scanned: {exc}") from exc
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if not _PROMISE.search(ast.get_docstring(fn) or ""):
                    continue
                for handler in ast.walk(fn):
                    if not isinstance(handler, ast.ExceptHandler):
                        continue
                    caught = handler.type
                    blanket = isinstance(caught, ast.Name) and caught.id in {
                        "Exception",
                        "BaseException",
                    }
                    if not blanket:
                        continue
                    line = lines[handler.lineno - 1] if handler.lineno <= len(lines) else ""
                    if re.search(r"noqa:\s*BLE001\s*-\s*\S", line):
                        continue  # carries its reason, the repo's convention
                    if _is_visible(handler):
                        continue
                    out.append(f"{rel}:{handler.lineno} in {fn.name}()")
    return sorted(out)


def test_the_scan_finds_functions_to_check():
    """A promise-matcher that matched nothing would make the ratchet vacuously green."""
    promises = 0
    for root in ROOTS:
        for path in scan_files(ROOT / root):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                raise AssertionError(f"{path} does not parse: {exc}") from exc
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and _PROMISE.search(
                    ast.get_docstring(fn) or ""
                ):
                    promises += 1
    assert promises > 40, f"only {promises} degradation promises found — the matcher is broken"


def test_no_new_silent_degradation():
    silent = _silent_sites()
    assert len(silent) <= BASELINE, (
        f"{len(silent)} silent degradations, baseline {BASELINE}. A documented fallback that says "
        "nothing is indistinguishable from the normal case — log the cause, return it to the "
        "caller, or put the reason on the except line as `# noqa: BLE001 - why`:\n  "
        + "\n  ".join(silent[BASELINE:] or silent)
    )


def test_the_baseline_is_not_stale():
    """When the count drops, lower the baseline — otherwise the ratchet stops ratcheting."""
    silent = _silent_sites()
    assert len(silent) >= BASELINE - 2, (
        f"only {len(silent)} silent sites remain against a baseline of {BASELINE}; lower BASELINE "
        "to lock the improvement in"
    )
