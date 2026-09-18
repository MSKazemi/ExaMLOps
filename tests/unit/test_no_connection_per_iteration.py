# tests/unit/test_no_connection_per_iteration.py
"""Opening the datastore inside a loop costs a connection per iteration.

On SQLite that is nearly free, which is why it survives review and why no unit test notices: every
assertion still passes, only slower. On Postgres each `get_db()` is a pool checkout and a round
trip — 3.3 ms pooled, 28.1 ms unpooled, measured in
[the backend guide](../../docs/guides/postgres-backend.md#connection-pooling) — so a loop over
models turns one report into one connection per model.

Measured on a local Postgres before this guard existed: `corruption.input_drift_rows()` opened
**101 connections for 50 models** (one for the model list, one per model for its snapshots, one per
model for its baseline) and took 24 ms. Sharing one connection made it **2 connections and 11 ms**
for identical output, and the saving grows with every millisecond of network distance — the
measurement above is against a server on the same host, which is the *smallest* the gap ever is.

This is a performance defect that behaves like correct code, so it needs a structural check rather
than a reviewer's attention. The scan is an AST walk, not a regex: `with get_db() as conn:` inside a
`for` is what matters, and a comment or a string that merely mentions it is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOTS = (
    "platform/cli/src/examlops",
    "platform/services",
    "serving",
    "pipelines",
)
#: Functions whose call opens (or takes) a datastore connection for the duration of a block.
OPENERS = {"get_db", "connect", "_immediate_write", "begin_immediate"}

#: file → (how many such sites it may have, why a connection *per iteration* is right there).
#:
#: The count is what keeps a file-level exemption from becoming a blanket one: a **new** site in an
#: already-exempted file still fails, because the number no longer matches. Every entry here would
#: be a defect if it were hoisted.
ALLOWED: dict[str, tuple[int, str]] = {
    "platform/cli/src/examlops/lifecycle/upgrade.py": (
        1,
        "each migration gets its own transaction on purpose: one that fails must not roll back the "
        "migrations already applied before it, and the loop is over a handful of steps run once",
    ),
    "platform/cli/src/examlops/telemetry_anchor.py": (
        1,
        "the connection is released before `write_audit_event`, which opens its own — holding one "
        "across a nested open is how a small pool deadlocks. The loop is over a fixed table list",
    ),
    "platform/services/control_plane/app.py": (
        2,
        "two sites, both deliberate: `_get_db` retries the open itself (a fresh connection per "
        "attempt is the point of a retry, bounded to three), and the reconciler gives each run its "
        "own transaction so one failure cannot roll back the runs before it and the admission lock "
        "is not held across the batch — on the control plane's own SQLite store, where a connection "
        "is nearly free and the batch is bounded by a LIMIT",
    ),
}


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.depth = 0
        self.hits: list[int] = []

    def _loop(self, node: ast.AST) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_For = visit_AsyncFor = visit_While = _loop  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        if self.depth:
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    func = call.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in OPENERS:
                        self.hits.append(node.lineno)
        self.generic_visit(node)

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        """`conn = get_db()` — the same defect without a `with` block.

        The first version of this guard only looked at context managers and missed every
        assignment form, which is how a connection opened per iteration in the control plane's
        reconcile loop went unseen. A guard that misses the commonest alternative spelling is a
        guard with a hole in it.
        """
        if self.depth and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in OPENERS or name.lstrip("_") in OPENERS:
                self.hits.append(node.lineno)
        self.generic_visit(node)


def _sites() -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    scanned = 0
    for root in ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts or "/tests/" in path.as_posix():
                continue
            scanned += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - a broken file fails loudly elsewhere
                continue
            finder = _Finder()
            finder.visit(tree)
            rel = path.relative_to(ROOT).as_posix()
            found.extend((rel, line) for line in finder.hits)
    assert scanned > 500, f"scanned only {scanned} files — the roots are stale, not the tree clean"
    return found


def test_no_new_site_opens_the_datastore_once_per_iteration():
    offenders = [f"{rel}:{line}" for rel, line in _sites() if rel not in ALLOWED]
    assert not offenders, (
        "these open a datastore connection inside a loop, which costs a pool checkout and a round "
        "trip per iteration on Postgres:\n  "
        + "\n  ".join(offenders)
        + "\nHoist the open outside the loop and pass `conn=` to the helpers it calls, or add it "
        "to ALLOWED with the reason a connection per iteration is right there."
    )


def test_an_exempted_file_may_not_grow_a_new_site():
    """A file-level exemption must not become a blanket one."""
    counts: dict[str, int] = {}
    for rel, _ in _sites():
        counts[rel] = counts.get(rel, 0) + 1
    grown = {
        rel: (counts.get(rel, 0), allowed)
        for rel, (allowed, _) in ALLOWED.items()
        if counts.get(rel, 0) > allowed
    }
    assert not grown, (
        f"exempted files gained sites (found, allowed): {grown}. The existing ones are deliberate; "
        "a new one needs its own justification — read it, then raise the count or hoist the open."
    )


def test_the_exemptions_still_describe_something_real():
    """An exemption for a site that no longer loops is an open door."""
    looping = {rel for rel, _ in _sites()}
    stale = sorted(set(ALLOWED) - looping)
    assert not stale, f"{stale} no longer open a connection in a loop; drop the exemption"


def test_the_scan_sees_a_loop_and_ignores_a_plain_block():
    """The guard is the AST walk; one that matched nothing would pass forever."""
    finder = _Finder()
    finder.visit(ast.parse("for x in y:\n    with get_db() as c:\n        c.execute('SELECT 1')\n"))
    assert finder.hits == [2]

    plain = _Finder()
    plain.visit(ast.parse("with get_db() as c:\n    for x in y:\n        c.execute('SELECT 1')\n"))
    assert plain.hits == [], "a connection held *around* a loop is the shape we want"
