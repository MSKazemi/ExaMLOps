# tests/unit/test_prompt_lineage_node.py
"""ADR 0009 clause 5's third linkage — a prompt version as an A2 lineage node.

The clause: "prompt version appears as a C1 span attribute, a C2 eval dimension, and an **A2
lineage node**". The span attribute exists (the gateway stamps `examlops.prompt.version`); the
lineage node did not.

The design question was *where*, not whether. A prompt version is an input to every gateway call
that resolves it, so emitting there would put one lineage event on the graph per inference — the
per-request lineage the platform deliberately does not do. A **label move** is the release event:
low-volume, decision-shaped, and the thing an operator asks about when a prompt changed what
production says. It mirrors the promotion event an alias move already emits for models.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.commands import prompt_cmd  # noqa: E402
from examlops.data.prompts import create_prompt_version, set_prompt_label  # noqa: E402
from examlops.lineage import prompt_node  # noqa: E402

runner = CliRunner()


def _events(job: str) -> list[dict]:
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lineage_events WHERE job=? ORDER BY id DESC", (job,)
            )
        ]


@pytest.fixture
def prompt(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_PROMPT_GATE_LABELS", raising=False)
    create_prompt_version("triage", "Classify: {text}")
    create_prompt_version("triage", "Classify carefully: {text}")
    set_prompt_label("triage", "prod", 1)
    return "triage"


# ── the node ──────────────────────────────────────────────────────────────────


def test_a_prompt_node_is_version_scoped():
    """A prompt version is an immutable artifact a label points at — like a model version."""
    node = prompt_node("triage", 3)
    assert node.name == "triage/v3"
    assert node.type == "prompt"


def test_the_node_type_is_its_own_not_a_model():
    assert prompt_node("t", 1).type != "model"


# ── emitted on the release, not per request ───────────────────────────────────


def test_moving_a_label_emits_lineage(prompt):
    result = runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])

    assert result.exit_code == 0
    events = _events("prompt-label:triage")
    assert events and events[0]["event_type"] == "COMPLETE"


def test_the_event_links_the_version_to_the_label(prompt):
    from examlops.platform_db import get_db

    runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])
    run_id = _events("prompt-label:triage")[0]["run_id"]

    with get_db() as conn:
        io_rows = [
            dict(r) for r in conn.execute("SELECT * FROM lineage_io WHERE run_id=?", (run_id,))
        ]

    inputs = [r["node_name"] for r in io_rows if r["direction"] == "input"]
    outputs = [r["node_name"] for r in io_rows if r["direction"] == "output"]
    assert any("triage/v2" in n for n in inputs)
    assert any("triage@prod" in n for n in outputs)


def test_a_rollback_is_recorded_as_one(prompt):
    """ "Which release put this back" is a different question from "which release shipped it"."""
    runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])
    runner.invoke(prompt_cmd.app, ["rollback", prompt, "prod", "1"])

    facets = str(_events("prompt-label:triage")[0]["facets_json"])

    assert "rollback" in facets and "true" in facets.lower()


def test_a_forward_move_is_not_flagged_as_a_rollback(prompt):
    runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])
    assert "false" in str(_events("prompt-label:triage")[0]["facets_json"]).lower()


def test_the_label_is_on_the_event(prompt):
    runner.invoke(prompt_cmd.app, ["label", prompt, "staging", "2"])
    assert "staging" in str(_events("prompt-label:triage")[0]["facets_json"])


def test_each_label_gets_its_own_run(prompt):
    runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])
    runner.invoke(prompt_cmd.app, ["label", prompt, "staging", "2"])

    run_ids = {e["run_id"] for e in _events("prompt-label:triage")}

    assert len(run_ids) == 2


# ── it does not emit where it would flood ─────────────────────────────────────


def test_resolving_a_prompt_emits_no_lineage(prompt):
    """One event per inference is the per-request lineage the platform deliberately avoids."""
    from examlops.prompts import get_prompt

    before = len(_events("prompt-label:triage"))
    for _ in range(5):
        get_prompt("triage", "prod")

    assert len(_events("prompt-label:triage")) == before


# ── fail-open ─────────────────────────────────────────────────────────────────


def test_a_broken_emitter_does_not_fail_a_completed_label_move(prompt, monkeypatch):
    """The label has already moved; bookkeeping must not report failure."""
    import examlops.lineage as lineage_mod

    monkeypatch.setattr(
        lineage_mod, "emit_lineage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )

    result = runner.invoke(prompt_cmd.app, ["label", prompt, "prod", "2"])

    assert result.exit_code == 0
    from examlops.data.prompts import get_prompt_by_label

    assert get_prompt_by_label("triage", "prod")["version"] == 2
