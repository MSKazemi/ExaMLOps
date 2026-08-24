"""A test that asserts nothing cannot fail, and a suite is only worth what its tests can detect.

Two tests in the dashboard storage suite were found this way: `test_ensure_bucket_creates_when_
missing` and `test_delete` called their subject and asserted nothing, so both stayed green with the
method under test replaced by `return`. Each named an observable effect — a bucket created, an
object deleted — that its body never looked at.

This guard makes the sweep durable. It fails on any test function with no assertion of any kind:
no `assert`, no `raise`, no `pytest.raises`/`warns`/`fail`, no mock `assert_*` call.

A test whose whole claim is "this does not raise" is legitimate, and the exemptions below name
each one individually with the reason. The list is deliberately explicit rather than a pattern:
a new assertion-free test should have to be argued for, which is the entire point.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_TEST_DIRS = (
    "tests",
    "platform/services/dashboard/backend/tests",
    "platform/services/control_plane/tests",
    "platform/services/agent/tests",
)

# Directories holding copies of source rather than source — scanning them makes this guard's
# answer depend on whether anyone has run a build.
_NOT_SOURCE = {"node_modules", ".venv", "build", "dist", "site-packages", ".git", "__pycache__"}

# Each entry is "<path>::<function>" and each is a test whose entire claim is that the call
# completes without raising. That claim is real, and an assertion would only restate it.
_ASSERTION_FREE_BY_DESIGN = {
    # The subject is a guard that raises on violation; completing quietly IS the pass condition.
    "tests/unit/test_live_service_guard.py::test_a_socket_the_test_opened_itself_is_left_alone",
    "platform/services/control_plane/tests/test_live_service_guard.py"
    "::test_a_socket_the_test_opened_itself_is_left_alone",
    "platform/services/dashboard/backend/tests/test_live_service_guard.py"
    "::test_a_socket_the_test_opened_itself_is_left_alone",
    "tests/unit/test_datastore_reachability.py::test_listening_socket_passes",
    "tests/unit/test_datastore_reachability.py::test_unparseable_dsn_is_left_for_psycopg_to_reject",
    "tests/unit/test_datastore_reachability.py::test_cases_libpq_handles_better_are_skipped",
    # Import smoke: the import statement is the assertion.
    "tests/unit/test_smoke.py::test_examlops_importable",
    "tests/unit/test_smoke.py::test_examlops_features_importable",
    "tests/unit/test_smoke.py::test_examlops_schemas_importable",
    # Validation helpers that signal failure by raising.
    "tests/unit/test_model_schema_registry.py::test_validate_features_passes_correct_list",
    # ORM round-trips where the schema constraint under test is what would raise.
    "platform/services/dashboard/backend/tests/test_models.py::test_can_insert_url_row",
    "platform/services/dashboard/backend/tests/test_models.py::test_can_insert_unset_secret_row",
    "platform/services/dashboard/backend/tests/test_models.py::test_can_insert_set_secret_row",
    "platform/services/dashboard/backend/tests/test_models.py::test_audit_row_insert",
}

_ASSERTION_HELPERS = (
    "assert_",  # mock's assert_called_with &c.
    "raises",
    "warns",
    "pytest.fail",
    "pytest.approx",
)


def _test_files() -> list[Path]:
    out: list[Path] = []
    for d in _TEST_DIRS:
        root = _ROOT / d
        if not root.exists():
            continue
        for f in root.rglob("test_*.py"):
            if _NOT_SOURCE & set(f.parts):
                continue
            out.append(f)
    return out


def _asserts_something(node: ast.AST) -> bool:
    if any(isinstance(n, (ast.Assert, ast.Raise)) for n in ast.walk(node)):
        return True
    body = ast.unparse(node)
    return any(h in body for h in _ASSERTION_HELPERS)


def _assertion_free() -> list[str]:
    found: list[str] = []
    for f in _test_files():
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere, loudly
            continue
        rel = f.relative_to(_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if not _asserts_something(node):
                found.append(f"{rel}::{node.name}")
    return found


def test_the_guard_actually_reads_tests():
    """Proof there was something to read — an empty scan would make every claim below vacuous."""
    files = _test_files()
    assert len(files) > 100, f"only found {len(files)} test files; the scan roots are wrong"


def test_no_test_asserts_nothing():
    offenders = sorted(set(_assertion_free()) - _ASSERTION_FREE_BY_DESIGN)
    assert not offenders, (
        "these test functions contain no assertion, no raise and no assert_* helper, so they "
        "pass whatever their subject does:\n  " + "\n  ".join(offenders) + "\n\n"
        "Either assert the effect the test name claims, or add it to _ASSERTION_FREE_BY_DESIGN "
        "with the reason its only claim is 'does not raise'."
    )


def test_the_exemption_list_has_no_dead_entries():
    """An exemption for a test that no longer exists hides the next one that needs arguing for."""
    stale = sorted(_ASSERTION_FREE_BY_DESIGN - set(_assertion_free()))
    assert not stale, f"exemptions no longer needed (remove them): {stale}"
