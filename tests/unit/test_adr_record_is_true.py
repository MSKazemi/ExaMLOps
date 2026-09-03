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


def test_the_flag_check_does_not_depend_on_the_interpreter(rows):
    """A guard that quietly weakens is worse than one that fails.

    The flag check used to work by importing ``examlops.cli.main`` into whichever
    interpreter happened to run the file. Under the venv it checked flags; under a bare
    ``python3`` the import raised, every flag check became "do not judge", and the report
    said **44** shipped-but-proposed ADRs where the venv said **43** — the extra one being
    ADR 0032, which names ``exa pipeline run --distributed``, a flag that does not exist.
    Same command, two answers, and the weaker one looked healthy.

    The options now come from the same ``exa --json docs`` tree the command names come
    from, so any interpreter gets the same number.
    """
    bare = subprocess.run(["python3", str(RECONCILE)], capture_output=True, text=True, timeout=600)
    assert bare.returncode == 0, bare.stderr
    line = [ln for ln in bare.stdout.splitlines() if "ALL exist" in ln]
    assert line, bare.stdout
    reported = int(line[0].rsplit(":", 1)[1])

    in_process = len([r for r in rows if not r["accepted"] and r["present"] and not r["absent"]])
    assert reported == in_process, (
        f"a bare python3 reports {reported} shipped-but-proposed ADRs and this process "
        f"computes {in_process} — the flag check is degrading silently again"
    )


def test_a_named_flag_that_does_not_exist_is_absent(reconciler):
    """`exa pipeline run --distributed` is named by ADR 0032 and has never existed."""
    cmds = reconciler.cli_commands()
    if not cmds:  # pragma: no cover - depends on the local install
        pytest.skip("the exa CLI is not installed in this environment")
    assert reconciler.artifact_exists("exa pipeline run --distributed", cmds) is False


def test_a_body_parsed_flag_still_counts_as_real(reconciler):
    """`exa pipeline promote` declares no `--if-rmse-lt`; it parses the family at runtime.

    An options-only check calls that real, documented flag a missing artifact and would
    mark ADR 0117 as naming something absent.
    """
    cmds = reconciler.cli_commands()
    if not cmds:  # pragma: no cover - depends on the local install
        pytest.skip("the exa CLI is not installed in this environment")
    assert reconciler.artifact_exists("exa pipeline promote --if-rmse-lt", cmds) is True


def test_a_bolded_status_does_not_escape_the_guard():
    """``- **Status:** **Accepted**`` must count as Accepted.

    Emphasis is how a human writes a status they care about. With the markers left in,
    ``head.startswith("accepted")`` was False, so bolding the word silently exempted an
    ADR from the Accepted-names-a-missing-artifact check.
    """
    mod = _load()
    for raw in (
        "- **Status:** Accepted",
        "- **Status:** **Accepted**",
        "- **Status:** __Accepted__",
    ):
        assert mod.status_of(raw + "\n").lower().startswith("accepted"), raw


def test_a_partially_implemented_adr_is_not_accepted():
    """The third status value must not be mistaken for the second."""
    mod = _load()
    status = mod.status_of(
        "- **Status:** **Partially implemented** — 2026-08-28 (x); not Accepted\n"
    )
    head = status.split("—")[0].strip().rstrip(".").lower()
    assert head == "partially implemented"
    assert not head.startswith("accepted")


def test_a_body_parsed_flag_is_recognised_from_a_declaration_not_from_prose():
    """`--if-rmse-lt` must match the declared `--if-<metric>-<op>` pattern.

    Before `exa docs` published the pattern, the only thing that saved this flag was the
    command's own help text. Prose can outlive the flag it describes, so a declaration is
    the stronger evidence and must be what carries the check.
    """
    mod = _load()
    mod.cli_commands()
    path = "exa pipeline promote"
    assert mod._matches_declared_pattern(path, "--if-rmse-lt")
    assert mod._matches_declared_pattern(path, "--if-accuracy-gte")
    assert not mod._matches_declared_pattern(path, "--dry-run")
    assert not mod._matches_declared_pattern(path, "--nonsense")


def test_the_pattern_is_published_in_the_machine_readable_tree():
    """An agent building a tool schema from `exa --json docs` must see the flag."""
    mod = _load()
    mod.cli_commands()
    node = mod._CLI_NODES.get("exa pipeline promote")
    assert node is not None
    dynamic = [o for o in node.get("options", []) if o.get("dynamic")]
    assert dynamic, "exa pipeline promote publishes no dynamic options"
    assert any("--if-" in o["opts"] for o in dynamic)


def test_a_swept_adr_has_not_since_become_accepted(rows):
    """A recorded sweep says "the match proves nothing"; Accepted says "it was built".

    Both cannot be true of the same ADR. Whoever flips the status owns removing the note,
    and this is what tells them.
    """
    contradictory = [r["adr"] for r in rows if r["swept"] and r["accepted"]]
    assert not contradictory, (
        "These ADRs carry a name-match-only reconciliation note and are also marked Accepted. "
        "Remove the note when the status changes:\n  " + "\n  ".join(contradictory)
    )


def test_the_sweep_claim_is_still_true(reconciler, rows):
    """The note claims every named artifact predates the ADR. That can stop being true.

    An ADR swept today can be built tomorrow — the artifact appears, the sweep silently
    becomes a false statement in the design record, and nothing says so. This is the arm
    that catches it, and it is the reason the sweep is a checkable note rather than a
    deleted row in a report.
    """
    cmds = reconciler.cli_commands()
    stale = []
    for row in rows:
        if not row["swept"]:
            continue
        adr_date = reconciler._git(
            ["log", "--format=%ad", "--date=short", "--", f"design/adr/{row['adr']}"]
        )
        if not adr_date:
            continue
        newer = {
            token: seen
            for token in row["present"]
            if (seen := reconciler.first_seen(token, cmds)) and seen > adr_date
        }
        if newer:
            stale.append(f"{row['adr']} (adr {adr_date}): {newer}")
    assert not stale, (
        "These ADRs are swept as name-match-only, but now name an artifact that appeared "
        "AFTER them — so the sweep note is no longer true and the ADR is decidable:\n  "
        + "\n  ".join(stale)
    )


def test_a_swept_adr_leaves_the_still_to_decide_queue(reconciler, tmp_path, monkeypatch):
    """Red-first: without the note the ADR is queued, with it the queue is one shorter."""
    adr_dir = tmp_path / "adr"
    adr_dir.mkdir()
    body = "# ADR 9001 — x\n\n- **Status:** Proposed\n{note}\n## Context\n\n`exa status`\n"
    monkeypatch.setattr(reconciler, "ADR_DIR", adr_dir)
    cmds = reconciler.cli_commands()

    (adr_dir / "9001-x.md").write_text(body.format(note=""))
    plain = reconciler.reconcile(cmds)[0]
    assert plain["present"] == ["exa status"]
    assert plain["swept"] == ""

    (adr_dir / "9001-x.md").write_text(body.format(note="- **Reconciliation:** name-match only."))
    marked = reconciler.reconcile(cmds)[0]
    assert marked["present"] == ["exa status"]
    assert marked["swept"] == "name-match only."


def test_the_reconciler_does_not_read_stale_build_artifacts():
    """`build/` and `dist/` hold copies of the package that can be months out of date.

    The matcher takes the first file whose path matches a token, so a stale copy can answer for
    the source. It cuts both ways, and the second way is worse: a newly added symbol reads as
    **absent** — noise, which is how a guard gets switched off — and a symbol deleted from source
    but still present in the artifact reads as **present**, which blinds the Accepted-ADR check
    that gates the build.

    Measured 2026-09-02: 242 of 1235 scanned files came from `platform/cli/build`, and one ADR
    (0023) was reporting a function absent that had been in the source all along.
    """
    import sys

    sys.path.insert(0, str(ROOT / "platform" / "ci"))
    import adr_reconcile

    stale = [
        s
        for s in adr_reconcile.FILE_STRS
        if any(part in {"build", "dist"} for part in s.split("/"))
    ]
    assert not stale, (
        f"the ADR reconciler is reading {len(stale)} build/dist artifact(s), e.g. {stale[:3]} — "
        "a stale copy can answer for the source in either direction. Add the directory to "
        "SKIP_DIRS in platform/ci/adr_reconcile.py."
    )
