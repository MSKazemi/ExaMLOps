"""A test nobody can run is documentation, not verification.

Tests that need something real — Docker, a kind cluster, a Keycloak, a Ray, a pgvector — are gated
on an environment variable so they skip in the ordinary run. That is right. What is not right is a
gate with **no runner**: no `make` target, no CI job, nothing but a command in the file's own
docstring. Such a test never executes, so the claim it makes is unchecked and its rot undetectable.

Found on 2026-09-15 while relying on `test_prometheus_optional_targets_live.py` to settle what
Prometheus does with a stopped DNS-discovered target. It proves it, and nothing had ever run it.

**The scope is the property, not a naming convention.** The first version of this guard looked for
`tests/integration/*_live.py` and gates matching `*LIVE*` — the shape of the files that prompted it.
It passed at zero while `tests/unit/test_pgvector_store.py` (nine tests on pgvector/SQLite ranking
parity), `EXAMLOPS_NATS_TEST_URL` and `EXAMLOPS_REDIS_TEST_URL` sat unrunnable, because none matched
that spelling. A guard scoped to where you were looking finds what you already found.

The rule keys on the **file**: a test file that can skip for want of an environment variable needs
some way to run it. Keying on the variable would flag `EXAMLOPS_POSTGRES_DR_DSN`, which is an
optional override — its file runs under `make chaos-drills` either way.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITES = ("tests/unit", "tests/integration")


def _gated_files() -> dict[str, set[str]]:
    """Test files that read an environment variable with **no default**, and the names they read.

    No default is what makes it a gate: the test cannot proceed without being told where the real
    thing is. A `getenv(name, fallback)` is configuration, not a gate.
    """
    gated: dict[str, set[str]] = {}
    for suite in SUITES:
        for path in sorted((ROOT / suite).rglob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            names = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) != 1:
                    continue
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else ""
                if attr not in ("getenv",) or node.keywords:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("EXAMLOPS_"):
                        names.add(arg.value)
            if names:
                gated[str(path.relative_to(ROOT))] = names
    return gated


def _runners() -> str:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text += workflow.read_text(encoding="utf-8")
    return text


def _without_a_runner() -> list[str]:
    """Gated files that neither the Makefile nor CI names, by path or by any of their gates."""
    runners = _runners()
    missing = []
    for path, names in _gated_files().items():
        if Path(path).name in runners or path in runners:
            continue
        if any(name in runners for name in names):
            continue
        missing.append(path)
    return sorted(missing)


# Lower this with the same commit that adds a runner; a new gated test without one pushes it up.
UNRUNNABLE_GATED_FILE_CEILING = 0


def test_every_gated_test_file_has_a_way_to_run_it():
    unrunnable = _without_a_runner()
    assert len(unrunnable) <= UNRUNNABLE_GATED_FILE_CEILING, (
        f"{len(unrunnable)} test file(s) skip for want of an environment variable and have no "
        f"`make` target and no CI job, so nothing ever runs them: {unrunnable}. Add a target "
        "beside `prometheus-live` in the Makefile."
    )


def test_the_ceiling_is_not_slack():
    assert len(_without_a_runner()) == UNRUNNABLE_GATED_FILE_CEILING, (
        f"the ceiling has drifted — lower it to {len(_without_a_runner())}"
    )


def test_the_inventory_is_not_empty():
    """A detector that matched nothing would make both checks above pass vacuously."""
    gated = _gated_files()
    assert len(gated) >= 15, f"only {len(gated)} gated test files found — is the rule still right?"
    assert any("pgvector" in path for path in gated), "the unit-suite gate is missing"
