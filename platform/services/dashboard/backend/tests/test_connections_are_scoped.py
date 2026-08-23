"""Guard: every connection this app opens must be released even when the body raises.

Under SQLite a connection that is opened and never closed is collected with the object, and the
cost of leaking one is a file handle. Under ``EXAMLOPS_DB_BACKEND=postgres`` it is a *pooled*
resource: a connection that is not returned is gone for the life of the process, and the tenth
one exhausts ``max_size``. Every later ``getconn()`` then waits the pool's full 30-second default
before failing, which is how a suite that leaked a handful of connections stopped looking like a
leak and started looking like a hang.

``conn.close()`` written as the last statement of a function is not enough — it runs only on the
happy path. The release has to be in a ``finally`` (or a ``with``). The baseline below started at
51 and is now 0: every site is scoped, so this is no longer a ratchet but a floor, and any new
unprotected connection fails the suite.

One shape this rules out is worth naming, because it read as correct: a handler that called
``conn.close()`` and then ``raise HTTPException(400, ...)`` released the connection on the happy
path and leaked it on every rejected request — the paths a caller can trigger at will.
"""

import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent

# Sites where ``conn.close()`` exists but is not reached when the body raises. Now zero, and it
# stays zero: put the close in a ``finally`` rather than raising this number.
_UNPROTECTED_BASELINE = 0


def _connect_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` for each ``connect()`` in *tree* that is not safely scoped."""
    found: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "connect" or not isinstance(node.targets[0], ast.Name):
                continue
            var = node.targets[0].id

            # A helper that hands the connection back is not the owner; its caller is.
            if any(
                isinstance(r, ast.Return) and isinstance(r.value, ast.Name) and r.value.id == var
                for r in ast.walk(fn)
            ):
                continue

            closes = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "close"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == var
            ]
            if not closes:
                found.append((node.lineno, "never closed"))
                continue
            finally_calls = {
                inner.lineno
                for t in ast.walk(fn)
                if isinstance(t, ast.Try)
                for b in t.finalbody
                for inner in ast.walk(b)
                if isinstance(inner, ast.Call)
            }
            if not any(c.lineno in finally_calls for c in closes):
                found.append((node.lineno, "close not in finally"))
    return found


def _scan() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    scanned = 0
    for py in sorted(_BACKEND.rglob("*.py")):
        if "__pycache__" in py.parts or "tests" in py.parts or py.name == "dbconn.py":
            continue
        scanned += 1
        sites = _connect_sites(ast.parse(py.read_text(), filename=str(py)))
        if sites:
            out[str(py.relative_to(_BACKEND))] = sites
    # Both tests below conclude something from an *absence*: no unreleased connection, no growth
    # in unprotected closes. An absence is only evidence if the search happened. Move the backend
    # and this walk returns nothing, both assertions hold trivially, and the guard reports green
    # while enforcing nothing — which reads exactly like a clean tree.
    assert scanned, f"scanned {_BACKEND} and found no Python files — the guard's path is stale"
    return out


def test_no_connection_is_opened_without_being_released():
    """A connection with no ``close()`` at all leaks on every call, not just on failure."""
    offenders = {
        path: [ln for ln, kind in sites if kind == "never closed"]
        for path, sites in _scan().items()
        if any(kind == "never closed" for _, kind in sites)
    }
    assert not offenders, (
        "these open a connection and never close it, which under Postgres removes it from the "
        f"pool permanently: {offenders}"
    )


def test_unprotected_close_count_does_not_grow():
    """``conn.close()`` outside a ``finally`` releases nothing when the body raises."""
    sites = _scan()
    count = sum(1 for s in sites.values() for _, kind in s if kind == "close not in finally")
    detail = {
        p: [ln for ln, k in s if k == "close not in finally"]
        for p, s in sites.items()
        if any(k == "close not in finally" for _, k in s)
    }
    assert count <= _UNPROTECTED_BASELINE, (
        f"{count} unprotected close() sites, baseline {_UNPROTECTED_BASELINE}. New code must put "
        f"the close in a finally: or use a with-block. Sites: {detail}"
    )
