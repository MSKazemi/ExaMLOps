"""The wide question bank must ask about real commands, and must not leak its own answers.

`operator_qa` reached 30/30, at which point a fixed 30-question suite stops measuring anything.
`cli_coverage` widens it to the whole CLI by reusing the hand-written **Use case** column of
`docs/reference/cli-commands-guide.md`. That only works if three things hold, and each is a way
the measurement could quietly become worthless rather than fail:

* every expected command still exists in the live CLI tree (else the agent is marked wrong for
  being right);
* no question contains an `exa …` command (else it hands over the answer);
* the sample is deterministic for a seed (else a moving rate is just a moving sample).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.evaluation import cli_coverage

runner = CliRunner()


def _live_commands() -> set[str]:
    """Every command and group in the running CLI tree, as ``exa a b c`` strings.

    Groups count: `exa audit` carries its own options and runs on its own, so the guide
    documents it as a command even though a leaf-only walk classes it as a branch.
    """
    result = runner.invoke(app, ["--json", "docs"])
    assert result.exit_code == 0, result.output
    found: set[str] = set()

    def visit(node: dict) -> None:
        name = node.get("name") or ""
        if name:
            found.add(name if name.startswith("exa") else f"exa {name}")
        for kid in node.get("subcommands") or []:
            visit(kid)

    visit(json.loads(result.stdout))
    return found


def test_the_bank_is_wide_enough_to_be_worth_running():
    s = cli_coverage.stats()
    # The guide covers the whole CLI; if the parser suddenly sees a handful of rows it has been
    # broken by a formatting change, and a tiny pool would still produce a plausible-looking rate.
    assert s["usable"] > 300, s
    assert s["total"] > 350, s


def test_every_expected_command_exists_in_the_live_cli():
    live = _live_commands()
    rows, _ = cli_coverage.load_rows()
    missing = sorted({r.command for r in rows} - live)
    assert not missing, f"the bank expects commands the CLI does not have: {missing[:10]}"


def test_no_question_hands_the_agent_its_own_answer():
    questions, _pool, dropped = cli_coverage.sample(0)
    leaking = [q.id for q in questions if "exa " in q.prompt.lower()]
    assert not leaking, f"questions naming a command: {leaking[:5]}"
    # The filter must be visible, not silent: something in the guide always leaks.
    assert dropped > 0


def test_sampling_is_deterministic_for_a_seed():
    a = [q.id for q in cli_coverage.sample(20, seed=3)[0]]
    b = [q.id for q in cli_coverage.sample(20, seed=3)[0]]
    c = [q.id for q in cli_coverage.sample(20, seed=4)[0]]
    assert a == b
    assert a != c, "two seeds drew the same sample — the seed is not reaching the sampler"


def test_a_command_with_placeholder_arguments_grades_on_its_name():
    # `exa explain [command …]` must expect `exa explain`, not the bracketed form, or a correct
    # answer scores zero for not repeating the guide's argument syntax.
    assert cli_coverage._clean_command("exa explain [command …]") == "exa explain"
    assert cli_coverage._clean_command("exa models diff <name> <a> <b>") == "exa models diff"
    # Uppercase metavars, flags and stray quotes are argument syntax too — matching the *name*
    # positively is what stops each new shape needing its own rule.
    assert (
        cli_coverage._clean_command("exa gateway key revoke KEY_HASH") == "exa gateway key revoke"
    )
    assert cli_coverage._clean_command("exa chat --session ID") == "exa chat"
    assert cli_coverage._clean_command('exa ask "') == "exa ask"


def test_the_description_mode_changes_the_question_and_is_off_by_default():
    plain = cli_coverage.sample(5, seed=2)[0]
    rich = cli_coverage.sample(5, seed=2, with_description=True)[0]
    assert all("described as" not in q.prompt for q in plain), "default must be the hard question"
    assert all("described as" in q.prompt for q in rich)
    # Even the easier mode must not hand over a command string: the leak filter is applied to
    # whatever text the question will actually show, so the invariant holds in both modes.
    for qs in (plain, rich):
        assert all("exa " not in q.prompt.lower().replace("`exa` command", "") for q in qs)
    # The description leaks more often than the use case, so the mode has a smaller pool.
    assert cli_coverage.stats(with_description=True)["usable"] < cli_coverage.stats()["usable"]


def test_a_miss_is_classified_by_whether_it_named_a_real_command():
    live = cli_coverage.live_commands()
    assert "exa serve check" in live and "exa autopilot disable" in live

    # The failure mode this split exists for: the agent named a real command that also fits the
    # question, which is a property of the question, not of the agent.
    assert (
        cli_coverage.classify_miss(
            "```bash\nexa autopilot disable\n```", "exa drift auto-retrain disable", live
        )
        == "named-another-real-command"
    )
    # A genuinely wrong answer must not be excused as ambiguity.
    assert cli_coverage.classify_miss("run exa frobnicate", "exa serve check", live) == (
        "named-no-real-command"
    )
    assert cli_coverage.classify_miss("I don't know", "exa serve check", live) == "named-nothing"
    # Naming *only* the expected command cannot be a graded miss; if it ever shows up, the
    # grader and the classifier disagree about the same answer, and that must be visible rather
    # than quietly folded into one of the other categories.
    assert cli_coverage.classify_miss("exa serve check", "exa serve check", live) == (
        "named-the-expected-command"
    )


def test_the_bank_is_asked_concurrently_so_the_full_pool_fits_in_one_sitting() -> None:
    """363 questions asked one at a time is over an hour; the runner must overlap them.

    Deterministic rather than timed: a barrier of width ``concurrency`` only releases when that
    many callers are inside ``ask`` at once, so a serial runner deadlocks instead of passing
    slowly.
    """
    import threading

    from examlops.evaluation.cli_coverage import ask_all, sample

    questions, _, _ = sample(12, seed=3)
    width = 4
    barrier = threading.Barrier(width, timeout=10)

    def ask(q: object) -> str:
        barrier.wait()
        return "exa status"

    answers, failures = ask_all(questions, ask, concurrency=width)
    assert failures == []
    assert len(answers) == len(questions)


def test_one_question_failing_does_not_lose_the_other_answers() -> None:
    from examlops.evaluation.cli_coverage import ask_all, sample

    questions, _, _ = sample(5, seed=3)
    bad = questions[2].id

    def ask(q: object) -> str:
        if getattr(q, "id") == bad:
            raise RuntimeError("boom")
        return "exa status"

    answers, failures = ask_all(questions, ask, concurrency=3)
    assert len(answers) == 4
    assert bad not in answers
    assert len(failures) == 1 and "boom" in failures[0]


def test_every_question_carries_a_distinct_id_so_none_is_silently_dropped() -> None:
    """Answers are collected in a dict keyed by question id, so a collision loses a question.

    The guide has flag-variant rows (``exa chat`` and ``exa chat --session ID``) that reduce to
    the same command name. Two real questions then shared one key and the full-pool run reported
    ``asked: 363, answered: 361`` with an *empty* failure list — the count was wrong by two and
    said nothing about it, which is the failure mode a measurement must not have.
    """
    from collections import Counter

    from examlops.evaluation.cli_coverage import sample

    for with_description in (False, True):
        questions, _, _ = sample(10**6, seed=0, with_description=with_description)
        dupes = {qid: n for qid, n in Counter(q.id for q in questions).items() if n > 1}
        assert dupes == {}, f"with_description={with_description}: {dupes}"


def test_a_real_command_followed_by_an_argument_is_still_a_real_command() -> None:
    """The classifier read the longest token run, so an argument hid the command inside it.

    Measured on the full-pool run of 2026-08-28: 3 of the 4 answers reported as
    ``named-no-real-command`` had in fact named a real command and then its argument
    (``exa drift input baseline JPCP``, ``exa eval gate run JPCP 18``,
    ``exa eval run operator-qa``). Only one — ``exa hardware list-pools`` — was invented. A
    classifier that calls a correct-but-different command an invention makes the agent look
    worse than it is, which is the same defect as making it look better.
    """
    from examlops.evaluation.cli_coverage import classify_miss

    live = {"exa drift input baseline", "exa eval gate", "exa hardware pools"}
    assert (
        classify_miss("run `exa drift input baseline JPCP`", "exa drift baseline", live)
        == "named-another-real-command"
    )
    assert (
        classify_miss("`exa hardware list-pools`", "exa hardware pools", live)
        == "named-no-real-command"
    )


def test_a_flag_the_command_does_not_have_is_reported() -> None:
    """Naming the right command with an invented flag is still an answer that fails when pasted.

    Grading stops at the command name, so this axis was unmeasured until 2026-08-28. Measured over
    the 363 real answers of that day's run: 319 flags, **one** invented (`exa project assign
    --ref`). The number is only worth having if it can move, hence the metric.
    """
    from examlops.evaluation.cli_coverage import unknown_flags

    live = {"exa project assign": {"--kind", "-k"}, "exa": {"--json", "--yes"}}
    assert unknown_flags("`exa project assign --ref foo`", live) == ["exa project assign --ref"]
    assert unknown_flags("`exa project assign --kind model`", live) == []
    # A global option is valid on every command, not just on `exa` itself.
    assert unknown_flags("`exa --json project assign --kind model`", live) == []


def test_a_flag_the_command_only_documents_still_counts_as_real() -> None:
    """`exa pipeline promote` parses `--if-<metric>-<op>` in its body rather than declaring each.

    An options-only check would call the product's real, documented flag an agent hallucination —
    the same rule the system-prompt guard already applies, and for the same reason.
    """
    from examlops.evaluation.cli_coverage import unknown_flags

    live = {"exa pipeline promote": {"--dry-run", "--if-rmse-lt"}, "exa": set()}
    assert unknown_flags("`exa pipeline promote jpcp --if-rmse-lt 5.0`", live) == []
