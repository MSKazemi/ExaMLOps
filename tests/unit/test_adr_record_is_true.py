"""The design record has to stay true, or it is worse than not having one.

An ADR marked `Accepted` is a statement that the system now works a certain way, and its
body names the artifacts that make it so — `exa` commands, `examlops.*` modules, file
paths. Nothing ever checked those existed. That let ADR 0034 sit in Accepted state for
months promising `exa agent memory` as the right-to-erasure surface while no such command
existed: the capability had shipped as `python -m skipper.memory_admin`, reachable only by
someone who knew where the agent's source tree was.

These guards make that class of drift fail the build instead of ageing quietly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECONCILE = ROOT / "platform" / "ci" / "adr_reconcile.py"


def _load():
    spec = importlib.util.spec_from_file_location("adr_reconcile", RECONCILE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reconciler():
    return _load()


@pytest.fixture(scope="module")
def rows(reconciler):
    return reconciler.reconcile(reconciler.cli_commands())


def test_no_accepted_adr_names_an_artifact_that_does_not_exist(rows):
    """An Accepted ADR is a claim about the built system. It has to be checkable."""
    lying = [(r["adr"], r["absent"]) for r in rows if r["accepted"] and r["absent"]]
    assert not lying, (
        "These ADRs are marked Accepted but name artifacts that do not exist. Either build "
        "them, or correct the ADR to name what actually shipped:\n"
        + "\n".join(f"  {adr}: {', '.join(missing)}" for adr, missing in lying)
    )


def test_the_guard_can_actually_fail(reconciler, tmp_path, monkeypatch):
    """The opposite arm: plant a lying ADR and the check must catch it.

    A guard that has only ever been observed passing is indistinguishable from a guard
    that cannot fail.
    """
    fake = tmp_path / "adr"
    fake.mkdir()
    (fake / "9999-invented.md").write_text(
        "# ADR 9999\n\n- **Status:** Accepted\n\n## Decision\n\n"
        "Ship `exa definitely-not-a-real-command` and `examlops.no_such_module`.\n"
    )
    monkeypatch.setattr(reconciler, "ADR_DIR", fake)
    result = reconciler.reconcile(reconciler.cli_commands())
    assert len(result) == 1
    assert result[0]["absent"], "a fabricated artifact was not reported as absent"


def test_a_rejected_alternative_is_not_a_broken_promise(reconciler, tmp_path, monkeypatch):
    """ADRs argue by naming what they did *not* build. That is not drift.

    ADR 0104 mentions `exa agent watch` only to reject it. Holding the record to artifacts
    it explicitly declined would make the guard un-passable and train everyone to ignore it.
    """
    fake = tmp_path / "adr"
    fake.mkdir()
    (fake / "9998-argued.md").write_text(
        "# ADR 9998\n\n- **Status:** Accepted\n\n## Decision\n\nUse `exa status`.\n\n"
        "## Alternatives considered\n\n- `exa definitely-not-a-real-command` — rejected.\n"
    )
    monkeypatch.setattr(reconciler, "ADR_DIR", fake)
    result = reconciler.reconcile(reconciler.cli_commands())
    assert result[0]["absent"] == [], (
        "an artifact named only as a rejected alternative was counted as a missing promise"
    )


def test_adr_0034_erasure_surface_is_reachable_from_the_cli():
    """The specific promise that went unbuilt: a person can erase what the agent remembers.

    Asserted against the CLI tree rather than the source, because ADR 0034's promise is
    about what an operator can *run*, not about a function existing somewhere.
    """
    exa = ROOT / ".venv" / "bin" / "exa"
    if not exa.exists():  # pragma: no cover - depends on the local install
        pytest.skip("the exa CLI is not installed in this environment")
    out = subprocess.run(
        [str(exa), "agent", "memory", "--help"], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    for verb in ("stats", "list", "export", "delete"):
        assert verb in out.stdout, f"`exa agent memory {verb}` is missing: {out.stdout}"


def test_the_reconciler_runs_as_a_command(rows):
    """It has to be usable by a person, not only by this test file."""
    out = subprocess.run(
        [sys.executable, str(RECONCILE)], capture_output=True, text=True, timeout=600
    )
    assert out.returncode == 0, out.stderr
    assert "ADRs:" in out.stdout
