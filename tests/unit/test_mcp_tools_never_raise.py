"""Every read tool in the MCP registry must degrade, not raise.

``test_mcp_capabilities`` states that contract in its module docstring but proves it against a
hand-written sample of ten tools, so a newly registered tool — or an old one that starts throwing
because a table, a service or an optional dependency is missing — is never checked. An agent calling
a raising tool gets a transport error instead of an answer it can reason about, which is exactly the
failure the ``ok``/``error`` envelope exists to prevent.

This walks the registry itself, so the guard grows with it: a read tool taking a parameter that
``SAMPLE_ARGS`` does not name fails here until a sample value is added.
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.mcp import tools as T  # noqa: E402

# One plausible value per parameter name used anywhere in the read surface. Values are deliberately
# ordinary rather than valid: a tool asked about something that does not exist must still answer.
SAMPLE_ARGS: dict[str, object] = {
    "model": "jpcp",
    "name": "jpcp",
    "dataset": "FData",
    "dataset_revision": "rev-does-not-exist",
    "flow_run_id": "00000000-0000-0000-0000-000000000000",
    "subject": "alice",
    "obj": "model:jpcp",
    "project": "research",
    "command": "exa status",
    "limit": 5,
}

READ_SPECS = [s for s in T.REGISTRY if not s.mutating]


@pytest.fixture
def db(tmp_path, monkeypatch, dead_services):
    # ``dead_services``: the tools reach HTTP endpoints as well as the database, and the
    # degrade path is what this module is about — see tests/unit/conftest.py.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "never_raise.db"))
    from examlops.platform_db import init_db

    init_db()
    yield


def _call_args(fn) -> dict[str, object]:
    """Required parameters only — optional ones exercise their own defaults."""
    args: dict[str, object] = {}
    for pname, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        assert pname in SAMPLE_ARGS, (
            f"{fn.__name__} takes a required parameter {pname!r} with no sample value; "
            f"add one to SAMPLE_ARGS so this guard keeps covering the whole registry"
        )
        args[pname] = SAMPLE_ARGS[pname]
    return args


def test_the_registry_is_not_trivially_small():
    # Guards the guard: an import or filter mistake that empties READ_SPECS would make every
    # parametrized case below vacuous.
    assert len(READ_SPECS) >= 40


@pytest.mark.parametrize("spec", READ_SPECS, ids=lambda s: s.name)
def test_read_tool_returns_an_envelope_instead_of_raising(spec, db):
    result = spec.fn(**_call_args(spec.fn))
    assert isinstance(result, dict), f"{spec.name} returned {type(result).__name__}, not a dict"
    assert "ok" in result or "error" in result, (
        f"{spec.name} returned a dict with neither 'ok' nor 'error': {sorted(result)[:6]}"
    )


def test_no_read_tool_reaches_the_real_platform_db(db):
    # The fixture points PLATFORM_DB at tmp_path; if a tool hardcoded a path instead, the repo's
    # own platform.db would be read during the suite.
    assert os.environ["PLATFORM_DB"].endswith("never_raise.db")
