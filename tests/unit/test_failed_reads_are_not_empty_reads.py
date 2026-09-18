"""A dashboard route may not gain a new way to serve a failure as an empty answer.

``except Exception: return []`` in a read route makes two very different states look identical to
the operator: *this register is empty* and *this query crashed*. On a governance or risk surface the
first is a claim — no model is in scope of the EU AI Act, no model is drifting, no SLO is defined —
so a missing table or an unreachable datastore was rendered as a clean bill of health, with nothing
in the log to say a read had failed at all.

Six such routes were converted on 2026-09-14 to ``readfail.readable()``, which logs the cause and
answers ``503`` — the convention those same routers already used when an *import* was unavailable.
The remaining handlers are measured here per file and may only **shrink**. Most are benign (an
optional enrichment, a best-effort probe); the point of the ratchet is that the population cannot
grow while it is worked down, and that a converted file cannot quietly revert.

Lower a file's entry — or delete it — in the same change that removes a handler;
``test_the_baseline_is_tight`` fails until you do.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTERS = ROOT / "platform/services/dashboard/backend/routers"

#: Per-file ceilings, measured 2026-09-14 after the six governance reads were converted.
#: Only ever lower these.
BASELINE: dict[str, int] = {
    "admission.py": 1,
    "autopilot.py": 1,
    "batch.py": 2,
    "cards.py": 2,
    "challenger.py": 1,
    "connections.py": 1,
    "containers.py": 3,
    "events.py": 1,
    "explain.py": 1,
    "feature_store.py": 1,
    "health.py": 2,
    "hpo.py": 2,
    "models.py": 3,
    "namespace.py": 2,
    "nextgen.py": 1,
    "platform_data.py": 3,
    "projects.py": 4,
    "prompts.py": 1,
    "providers.py": 1,
    "quality.py": 1,
    "rollback.py": 1,
    "scaling.py": 2,
    "shadow.py": 2,
    "sso.py": 1,
    "traffic.py": 2,
    "workbenches.py": 1,
}

#: The routes converted on 2026-09-14. Each answers a question whose empty answer is a claim, so
#: none of them may hold a silent handler again — their ceiling is zero by absence from BASELINE.
CONVERTED = ("compliance.py", "drift_data.py", "fairness.py", "slo.py")


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None or (isinstance(t, ast.Name) and t.id == "Exception"):
        return True
    return isinstance(t, ast.Tuple) and any(getattr(e, "id", None) == "Exception" for e in t.elts)


def count_silent_handlers(source: str) -> int:
    """Broad handlers that neither re-raise nor log, and answer with a value anyway.

    "Answers with a value" is ``return``/``pass``/``continue`` — the shapes that leave the caller
    unable to tell the failure happened. A handler that logs is out of scope even if it returns:
    the failure is at least recorded somewhere an operator can find it.
    """
    found = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        if any(isinstance(x, ast.Raise) for x in ast.walk(node)):
            continue
        dumped = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if any(k in dumped for k in ("logger", "logging", "warning", "exception")):
            continue
        body = node.body
        if (len(body) == 1 and isinstance(body[0], ast.Pass | ast.Continue)) or any(
            isinstance(x, ast.Return) for x in body
        ):
            found += 1
    return found


def _measure() -> dict[str, int]:
    counts = {}
    for path in sorted(ROUTERS.glob("*.py")):
        n = count_silent_handlers(path.read_text())
        if n:
            counts[path.name] = n
    return counts


def test_the_detector_sees_a_planted_offender_and_not_an_honest_handler():
    """Anti-vacuity: the measurement must react to the thing it claims to measure.

    A guard whose detector silently matches nothing passes forever and protects nothing, so it is
    fed all four shapes before any conclusion rests on it.
    """
    offender = "def f():\n    try:\n        g()\n    except Exception:\n        return []\n"
    assert count_silent_handlers(offender) == 1

    bare = "def f():\n    try:\n        g()\n    except:\n        pass\n"
    assert count_silent_handlers(bare) == 1, "a bare `except:` is the same defect"

    logged = (
        "def f():\n    try:\n        g()\n"
        "    except Exception:\n        logger.warning('x')\n        return []\n"
    )
    assert count_silent_handlers(logged) == 0, "a logged failure is not silent"

    narrow = "def f():\n    try:\n        g()\n    except KeyError:\n        return []\n"
    assert count_silent_handlers(narrow) == 0, "a named exception is a handled case, not a swallow"

    reraises = "def f():\n    try:\n        g()\n    except Exception as exc:\n        raise Boom from exc\n"
    assert count_silent_handlers(reraises) == 0


def test_no_router_gains_a_silent_failure_path():
    grown = []
    for name, n in _measure().items():
        ceiling = BASELINE.get(name, 0)
        if n > ceiling:
            grown.append(f"{name}: {ceiling} -> {n}")
    assert not grown, (
        "a dashboard read gained a way to serve a failure as an empty answer:\n  "
        + "\n  ".join(grown)
        + "\n\nUse `readfail.readable('<the surface>')` instead — it logs the cause and answers 503, "
        "which the console renders as an error with retry rather than as an empty register."
    )


def test_the_converted_governance_reads_stay_converted():
    """The six routes whose empty answer is a regulatory or safety claim hold no silent handler."""
    offenders = {
        name: count_silent_handlers((ROUTERS / name).read_text())
        for name in CONVERTED
        if count_silent_handlers((ROUTERS / name).read_text())
    }
    assert not offenders, (
        f"these reverted to serving a failed read as an empty one: {offenders}. "
        "Empty here means 'no model is in scope of the EU AI Act' / 'no model is drifting' / "
        "'no SLO is defined' — a claim, and it must not be produced by a broken query."
    )


def test_the_baseline_is_tight():
    """A file that no longer offends must leave the baseline, or the count can silently come back."""
    measured = _measure()
    stale = {name: n for name, n in BASELINE.items() if measured.get(name, 0) < n}
    assert not stale, (
        "these ceilings are above what the tree actually has — lower or delete them:\n  "
        + "\n  ".join(
            f"{name}: {n} -> {measured.get(name, 0)}" for name, n in sorted(stale.items())
        )
    )
