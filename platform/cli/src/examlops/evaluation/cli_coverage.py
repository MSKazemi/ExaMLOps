"""Measure the agent across the *whole* CLI surface, not 30 curated questions.

``operator_qa`` reached 30/30 on 2026-08-28, which is the point at which a fixed 30-question
suite stops being a measurement: it can no longer detect an improvement, and it can only detect
a regression in the 30 places it happens to look. The CLI has **372** leaf commands.

This module builds the missing wide measurement out of something the repo already maintains and
already guards: ``docs/reference/cli-commands-guide.md``, whose tables carry a hand-written
**Use case** sentence for every command ("When you're learning the CLI and want the why/when…").
That column is operator *intent* written by a human — not the command's own ``--help`` summary —
so turning it into "which command do I use when I need to <use case>?" is a real question rather
than a paraphrase of the answer.

Two properties make the result trustworthy rather than merely large:

* **Rows that leak their own answer are dropped.** Some use cases name the command, or name a
  neighbouring one (``exa doctor``'s reads "When ``exa status`` looks wrong…"). A question whose
  text contains the expected command measures nothing, so :func:`load_rows` excludes it and
  :func:`stats` reports how many were dropped — a silent filter would inflate the rate.
* **Grading stays deterministic and necessary-not-sufficient**, exactly as in ``operator_qa``:
  naming the command does not prove the answer was good, but not naming it proves it was not.
  No judge model, so nothing here needs judge calibration (ADR 0111).

The guide is regenerated and coverage-guarded by ``tests/unit/test_cli_guide_coverage.py``, so
this question bank cannot silently drift away from the CLI it measures.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from examlops.evaluation.operator_qa import Question, normalise

#: Default location of the hand-written command guide, relative to the repo root.
GUIDE = Path("docs/reference/cli-commands-guide.md")

# A guide row: | `exa foo bar` | what it does | use case | example |
_ROW = re.compile(r"^\|\s*`(exa [^`]+)`\s*\|([^|]*)\|([^|]*)\|")
# The command name is the leading run of lowercase words after ``exa``. Everything the guide
# adds after it is argument syntax, in several shapes — ``[command …]``, ``<name> <a> <b>``,
# ``KEY_HASH``, ``--session ID``, a stray quote — and none of it is part of the name. Matching
# the name positively is what makes that list not need to be complete; matching the *syntax*
# negatively let `exa ask "` and `exa gateway key revoke KEY_HASH` through as command names.
_NAME = re.compile(r"^exa(?: [a-z][a-z0-9-]*)+")


@dataclass(frozen=True)
class Row:
    """One command and the human-written intent that should lead an operator to it."""

    command: str
    what: str
    use_case: str


def _clean_command(raw: str) -> str:
    """``exa explain [command …]`` → ``exa explain``; the placeholder is not part of the name."""
    m = _NAME.match(raw.strip())
    return m.group(0) if m else raw.strip()


def _strip_markup(cell: str) -> str:
    """Guide cells carry backticks, bold and ``<br>``; the question needs plain prose."""
    text = cell.replace("<br>", " ")
    text = re.sub(r"[`*]", "", text)
    return " ".join(text.split()).strip()


def _leaks(use_case: str, command: str) -> bool:
    """True when the question text already contains an ``exa …`` command.

    Any command, not just the expected one: a use case that names a neighbour tells the agent
    which family to answer from, which is a different (easier) question than the one being asked.
    """
    return "exa " in use_case.lower() or command.lower() in use_case.lower()


def load_rows(
    guide: Path | None = None, *, with_description: bool = False
) -> tuple[list[Row], int]:
    """Every usable ``(command, what, use_case)`` row, plus the count dropped as leaking.

    The leak filter is applied to **exactly the text the question will show**, so the invariant
    "no question contains an ``exa …`` command" holds in both modes. The *What it does* cells
    leak more often than the use cases do, so the dropped count is mode-dependent — reported,
    not hidden, for the same reason the base count is.
    """
    path = guide or GUIDE
    rows: list[Row] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        command = _clean_command(m.group(1))
        what = _strip_markup(m.group(2))
        use_case = _strip_markup(m.group(3))
        if not use_case or len(use_case) < 20:
            dropped += 1
            continue
        shown = f"{use_case} {what}" if with_description else use_case
        if _leaks(shown, command):
            dropped += 1
            continue
        rows.append(Row(command=command, what=what, use_case=use_case))
    return rows, dropped


def _qid(command: str) -> str:
    return command.replace(" ", "-")


def to_question(row: Row, *, with_description: bool = False) -> Question:
    """Turn one guide row into a gradeable question.

    The prompt asks for *the* command and says the answer must name it, because the measurement
    is "did it name the command"; leaving that implicit penalises an answer that is correct but
    discursive, which would make the number about verbosity rather than knowledge.

    ``with_description`` adds the guide's *What it does* cell. It changes what is being measured,
    so it is a deliberate choice rather than a default. Measured 2026-08-28 on a 40-command
    sample: without it the agent scored 37/40, and **all three** misses were a different *real*
    command that also fits the use-case sentence once that sentence is read outside its table
    row ("Pause automation during an investigation" → ``exa autopilot disable``; "Produce
    release / governance documentation for a model" → ``exa governance report``; "Compare
    Production vs Canary head-to-head on live traffic" → ``exa serve ab analyze``). All three
    flip to correct with the description. So the two settings answer two different questions —
    *can the agent pick the intended command from operator intent alone* (hard, with an
    ambiguity floor) versus *can it map a described capability to its command* (clean, but the
    description paraphrases the command name). Neither number is the honest one on its own.
    """
    intent = row.use_case
    if with_description:
        intent = f"{row.use_case} The command I want is described as: {row.what}"
    return Question(
        id=_qid(row.command),
        category=row.command.split()[1] if len(row.command.split()) > 1 else "exa",
        prompt=(
            f"Which single `exa` command should I use when I need to: {intent} "
            f"Answer with the exact command."
        ),
        must_mention=((row.command,),),
        note=row.what,
    )


def sample(
    n: int,
    *,
    seed: int = 0,
    guide: Path | None = None,
    with_description: bool = False,
) -> tuple[list[Question], int, int]:
    """``n`` questions drawn deterministically, plus the pool size and the dropped count.

    Deterministic by seed so two runs are comparable: a rate that moves because a different
    sample was drawn is not a measurement of the agent.
    """
    rows, dropped = load_rows(guide, with_description=with_description)
    pool = len(rows)
    if n >= pool:
        chosen = rows
    else:
        chosen = random.Random(seed).sample(rows, n)
    chosen = sorted(chosen, key=lambda r: r.command)
    return (
        _with_distinct_ids([to_question(r, with_description=with_description) for r in chosen]),
        pool,
        dropped,
    )


def _with_distinct_ids(questions: list[Question]) -> list[Question]:
    """Suffix ids that collide, so no question is silently lost.

    The id is derived from the command name, and the guide carries flag-variant rows for the same
    command (``exa chat`` and ``exa chat --session ID``). Those are two different questions with
    the same expected answer and both are worth asking — but answers are collected in a dict keyed
    by id, so before this the second one overwrote the first and the run reported ``asked: 363,
    answered: 361`` with an empty failure list. A count that is wrong and silent is worse than one
    that is smaller.
    """
    seen: dict[str, int] = {}
    out: list[Question] = []
    for q in questions:
        seen[q.id] = n = seen.get(q.id, 0) + 1
        out.append(q if n == 1 else replace(q, id=f"{q.id}-{n}"))
    return out


def stats(guide: Path | None = None, *, with_description: bool = False) -> dict[str, int]:
    """Pool size and how many guide rows were unusable — reported, never hidden."""
    rows, dropped = load_rows(guide, with_description=with_description)
    return {"usable": len(rows), "dropped": dropped, "total": len(rows) + dropped}


# ── telling a wrong answer apart from an ambiguous question ───────────────────

#: A command word starts with a letter, so ``exa --json`` does not read as a command named
#: ``--json``. Same rule as ``operator_qa._COMMAND``, for the same reason.
#: An ``exa …`` invocation as an agent writes it, flags and arguments included.
_INVOCATION = re.compile(r"exa(?:\s+[^\s`\"'\n]+)*")
#: A long flag as it appears in help text (``--if-<metric>-<op>`` included).
_FLAG = re.compile(r"--[a-z0-9<>-]+")

_ANSWERED = re.compile(r"exa(?: [a-z][a-z0-9-]*)+")


def _longest_live_prefix(named: str, live: set[str]) -> str | None:
    """The longest prefix of ``named`` that is a real command, or ``None`` if there is none."""
    parts = named.split()
    for end in range(len(parts), 1, -1):
        candidate = " ".join(parts[:end])
        if candidate in live:
            return candidate
    return None


def classify_miss(answer: str, expected: str, live: set[str]) -> str:
    """Why a miss missed: a *different real command*, or no command at all.

    Measured 2026-08-28, this is the difference between the two things a low score can mean.
    All three misses in the first 40-question run named a real command that also fits the
    use-case sentence read outside its table row (``exa autopilot disable`` for "Pause
    automation during an investigation"), which is a property of the question, not of the agent.
    An answer that names no real command — or invents one — is the agent being wrong.

    Reporting one number without this split is how an ambiguity floor gets read as a quality
    problem, and how a real regression gets excused as ambiguity.
    """
    # Same normalisation the grader uses, or the classifier disagrees with it on exactly the
    # answers where a global option sits between ``exa`` and the subcommand.
    named = {m.group(0) for m in _ANSWERED.finditer(normalise(answer))}
    # The regex is greedy, so "exa eval gate run jpcp" matches whole and is not a live command
    # even though "exa eval gate" is. Reduce each match to its longest live prefix before
    # judging it, or an answer that names the right command and then its argument is scored as
    # an invention. Measured: 3 of the 4 "invented" answers in the full-pool run were this.
    real = {longest for c in named if (longest := _longest_live_prefix(c, live))}
    if real - {expected}:
        return "named-another-real-command"
    if expected in real:
        # Unreachable from a graded miss, and that is the point: it means the grader and this
        # classifier disagree about the same answer, which is a bug worth seeing rather than a
        # category to quietly fold into one of the others.
        return "named-the-expected-command"
    if named:
        return "named-no-real-command"
    return "named-nothing"


def live_commands() -> set[str]:
    """Every command and group in the running CLI tree, as ``exa a b c`` strings."""
    import typer.main

    from examlops.cli.commands.docs_cmd import _walk
    from examlops.cli.main import app

    tree = _walk(typer.main.get_command(app), ["exa"])
    found: set[str] = set()

    def visit(node: dict) -> None:
        name = str(node.get("name") or "")
        if name:
            found.add(name if name.startswith("exa") else f"exa {name}")
        for kid in node.get("subcommands") or []:
            visit(kid)

    visit(tree)
    return found


def ask_all(
    questions: Sequence[Question],
    ask: Callable[[Question], str],
    *,
    concurrency: int = 4,
    timings: dict[str, float] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Put every question to ``ask``, ``concurrency`` at a time, and collect what came back.

    The bank is 363 questions. Asked one at a time at the ~10 s an agent turn costs, the full-pool
    run is over an hour — long enough that it never gets run, which is the same as not having the
    measurement. Overlapping the calls is what makes the *whole* surface affordable to measure
    rather than a sample of it.

    A question that raises is recorded in the returned failure list and its answer is simply
    absent, because losing the other 362 answers to one timeout would be a worse trade than
    reporting a partial run honestly (the caller prints ``asked`` and ``answered`` separately).
    """
    answers: dict[str, str] = {}
    failures: list[str] = []
    lock = threading.Lock()

    def one(q: Question) -> None:
        started = time.perf_counter()
        try:
            text = ask(q)
        except Exception as exc:  # noqa: BLE001 - one bad question must not end the run
            with lock:
                failures.append(f"{q.id}: {type(exc).__name__}: {exc}")
            return
        with lock:
            answers[q.id] = text
            # Wall-clock per answer, for the caller's latency percentiles. Timed under
            # `concurrency`, so it is the latency an operator sees when the agent is loaded —
            # which is the number that matters, not a quiet-system best case.
            if timings is not None:
                timings[q.id] = time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(one, questions))
    return answers, failures


def live_options() -> dict[str, set[str]]:
    """Every command in the running CLI tree mapped to the flags it accepts.

    A flag counts as real if the command **declares** it or its own help **documents** it. The
    second arm is not laziness: `exa pipeline promote` parses ``--if-<metric>-<op> <value>`` in its
    body rather than declaring each one, so an options-only check would report the product's real,
    documented flag as an agent hallucination. It is the same rule the system-prompt guard applies,
    for the same reason — what it still catches is an invented flag, which is the risk.
    """
    import typer.main

    from examlops.cli.commands.docs_cmd import _walk
    from examlops.cli.main import app

    tree = _walk(typer.main.get_command(app), ["exa"])
    out: dict[str, set[str]] = {}

    def visit(node: dict) -> None:
        name = str(node.get("name") or "")
        if name:
            key = name if name.startswith("exa") else f"exa {name}"
            flags = set()
            help_text = str(node.get("help") or "")
            for opt in node.get("options") or []:
                for part in str(opt.get("opts") or "").split(","):
                    part = part.strip().split()[0] if part.strip() else ""
                    if part.startswith("-"):
                        flags.add(part)
                help_text += "\n" + str(opt.get("help") or "")
            flags |= set(_FLAG.findall(help_text))
            out[key] = flags
        for kid in node.get("subcommands") or []:
            visit(kid)

    visit(tree)
    return out


def unknown_flags(answer: str, options: dict[str, set[str]]) -> list[str]:
    """Every ``<command> <flag>`` in ``answer`` naming a flag that command does not accept.

    Grading stops at the command name, so an answer can name exactly the right command and hand
    the operator a flag that does not exist — which fails the moment it is pasted. This is the
    next layer down from "did it name the command", and it is measured the same way: necessary,
    not sufficient.
    """
    global_flags = options.get("exa", set())
    bad: list[str] = []
    for match in _INVOCATION.finditer(answer):
        tokens = match.group(0).split()
        words = [t for t in tokens if not t.startswith("-")]
        command = None
        for end in range(len(words), 0, -1):
            candidate = " ".join(words[:end])
            if candidate in options:
                command = candidate
                break
        if command is None:
            continue
        allowed = options[command] | global_flags
        for token in tokens:
            if not token.startswith("--"):
                continue
            flag = token.split("=")[0].rstrip(".,;)`'\"")
            if flag not in allowed:
                bad.append(f"{command} {flag}")
    return bad
