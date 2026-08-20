"""The operator question set must stay true, well-formed, and actually discriminating.

The set exists to measure the agent, so its own defects are invisible unless something checks
them: an expectation naming a command that does not exist would fail forever and read as an agent
bug, and an expectation nothing could fail would inflate the pass-rate.
"""

from __future__ import annotations

import typer

from examlops.evaluation import operator_qa as oq


def _cli_commands() -> set[str]:
    """Every leaf and group path in the live CLI, as ``exa a b`` strings."""
    from examlops.cli.main import app

    root = typer.main.get_command(app)
    paths: set[str] = set()

    def walk(node, path: list[str]) -> None:
        subs = getattr(node, "commands", None)
        if path:
            paths.add("exa " + " ".join(path))
        for name, sub in (subs or {}).items():
            walk(sub, path + [name])

    walk(root, [])
    return paths


def test_every_expected_command_exists():
    """An expectation may not name a command the CLI does not have."""
    real = _cli_commands()
    bogus = sorted(c for c in oq.referenced_commands() if c not in real)
    assert not bogus, (
        "the question set expects commands that do not exist:\n  "
        + "\n  ".join(bogus)
        + "\nThese would fail against any agent, however good, and read as its fault."
    )


def test_the_set_is_well_formed():
    qs = oq.OPERATOR_QUESTIONS
    assert len(qs) >= 30, f"expected a set of at least 30 questions, got {len(qs)}"
    ids = [q.id for q in qs]
    assert len(ids) == len(set(ids)), "duplicate question ids"
    for q in qs:
        assert q.prompt.strip().endswith("?"), f"{q.id}: prompt is not a question"
        assert q.must_mention, f"{q.id}: no expectation — it could never fail"
        for group in q.must_mention:
            assert group and all(a.strip() for a in group), f"{q.id}: empty expectation group"
    assert len({q.category for q in qs}) >= 6, "the set should span the platform, not one corner"


def test_grading_discriminates():
    """A right answer scores 1.0, a wrong one scores 0.0, a partial one scores between."""
    q = oq.by_id()["serve-canary"]
    ev = oq.MentionsAll(questions=oq.by_id())

    good = "Use `exa serve traffic JPCP --production 90 --canary 10` to split traffic."
    bad = "Just edit the config file and restart everything."
    partial = "Run exa serve traffic to change the split."

    def score(answer: str) -> float:
        return ev.score(oq.to_items({q.id: answer})[0]).score

    assert score(good) == 1.0
    assert score(bad) == 0.0
    assert 0.0 < score(partial) < 1.0, "a half-right answer must not score as fully right"


def test_unknown_question_scores_zero_and_says_why():
    """A stray item must not silently count as a pass."""
    from examlops.evaluation import EvalItem

    ev = oq.MentionsAll(questions=oq.by_id())
    s = ev.score(EvalItem(output="anything", metadata={"question_id": "nope"}))
    assert s.score == 0.0 and "unknown question" in s.detail["error"]


def test_an_empty_answer_never_passes():
    """A dead backend returns empty strings; that must score 0, not vacuously pass."""
    ev = oq.MentionsAll(questions=oq.by_id())
    for item in oq.to_items({q.id: "" for q in oq.OPERATOR_QUESTIONS}):
        assert ev.score(item).score == 0.0
