"""A fixed question set for measuring how well the agent answers operator questions.

The agent (Skipper) is meant to let a human "ask any kind of question about ExaMLOps". Nobody
had ever measured whether it does. This module is the measuring stick: a fixed set of questions
a new operator actually asks on their first week, each with a *deterministic* expectation, so a
pass-rate is a number rather than an impression.

Deliberately not an LLM judge. Grading here is "does the answer name the right command / concept",
checked by literal match, for three reasons: it needs no model to score (so the suite itself is
unit-testable, and a run costs one call per question rather than two), it cannot drift as a judge
model changes, and per ADR 0111 an uncalibrated judge may not gate anything anyway.

The expectations are *necessary*, not sufficient: naming `exa drift status` does not prove the
answer was good, but not naming it proves it was not. That asymmetry is the point — this catches
regressions and blind spots, it does not certify quality.

Every command named in an expectation is checked against the live CLI tree by
``tests/unit/test_operator_qa.py``, so this file cannot quietly start asserting a command that
does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from examlops.evaluation import EvalItem, Score

# A group is a set of acceptable alternatives; the answer must satisfy *every* group.
MentionGroups = tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Question:
    """One operator question and what any correct answer must contain."""

    id: str
    category: str
    prompt: str
    must_mention: MentionGroups
    note: str = ""

    def missing(self, answer: str) -> list[tuple[str, ...]]:
        """The expectation groups this answer fails to satisfy."""
        low = normalise(answer)
        return [g for g in self.must_mention if not any(alt.lower() in low for alt in g)]


# Global options may sit between ``exa`` and the subcommand — ``exa --json docs``,
# ``exa -o yaml status``, ``exa --yes retrain``, all valid and all documented. Literal substring
# grading read those as *not* naming the command: measured 2026-08-28, the answer
# ``exa --json --yes agent memory delete pref`` scored 0 against ``exa agent memory delete``,
# which is the same command. Normalising the invocation before matching is a correctness fix,
# not a leniency one — the expected command really is present, spelled the way the CLI accepts.
#: The two global options that consume the token after them; the rest are flags.
_GLOBAL_WITH_VALUE = ("--output", "-o", "--context", "-c")


def normalise(answer: str) -> str:
    """Lower-case ``answer`` with global options removed from every ``exa …`` invocation."""
    low = answer.lower()

    def strip(match: re.Match[str]) -> str:
        parts = match.group(0).split()
        out = [parts[0]]
        i = 1
        while i < len(parts):
            tok = parts[i]
            if tok.startswith("-"):
                # ``-o json`` consumes its value; ``-o=json`` and bare flags do not.
                if tok in _GLOBAL_WITH_VALUE and i + 1 < len(parts):
                    i += 1
                i += 1
                continue
            break
        out.extend(parts[i:])
        return " ".join(out)

    return re.sub(r"\bexa(?: \S+)*", strip, low)


OPERATOR_QUESTIONS: tuple[Question, ...] = (
    # ── orientation ───────────────────────────────────────────────────────────
    Question(
        "orient-status",
        "orientation",
        "I just got access. How do I see whether the platform is healthy?",
        ((" exa status", "`exa status`", "exa status"),),
    ),
    Question(
        "orient-discover",
        "orientation",
        "How do I find out what commands exist without reading the source?",
        (("exa --help", "exa -h", "exa docs", "exa explain"),),
    ),
    Question(
        "orient-config",
        "orientation",
        "How do I see which endpoints my CLI is actually pointed at, and where those came from?",
        (("exa env",),),
    ),
    Question(
        "orient-context",
        "orientation",
        "I work against a dev stack and a production one. How do I switch between them?",
        (("exa config use", "context"),),
    ),
    # ── training ──────────────────────────────────────────────────────────────
    Question(
        "train-run",
        "training",
        # Re-worded 2026-08-28: the old phrasing ("without touching the cluster") was also a
        # true description of `exa retrain --dummy`, which goes through the control plane and
        # never reaches the scheduler. Naming the *shell* excludes the HTTP path, so exactly
        # one command answers. Widening the accepted set instead would have made the grader
        # accept anything plausible, which is the opposite of what it is for.
        "How do I run the training pipeline directly from my own shell, with dummy data "
        "so that no cluster job is submitted?",
        (("exa pipeline run",), ("--dummy", "dummy")),
    ),
    Question(
        "train-list",
        "training",
        "Which models can I train?",
        (("exa pipeline list", "exa models list"),),
    ),
    Question(
        "train-new",
        "training",
        "How do I add a brand-new model to the platform?",
        (("exa scaffold",),),
    ),
    Question(
        "train-retrain",
        "training",
        "A model looks stale. How do I retrain it?",
        (("exa retrain", "exa pipeline run"),),
    ),
    Question(
        "train-pin-data",
        "training",
        "How do I make a training run reproducible against an exact dataset version?",
        (("exa data snapshot", "dataset revision", "--dataset-revision"),),
    ),
    # ── registry & promotion ──────────────────────────────────────────────────
    Question(
        "reg-compare",
        "registry",
        "How do I compare two versions of a model before promoting one?",
        (("exa models diff",),),
    ),
    Question(
        "reg-lineage",
        "registry",
        "Where did this model version come from — which pipeline and which data?",
        (("exa models lineage",),),
    ),
    Question(
        "reg-promote",
        "registry",
        "How do I promote a model to Production only if its RMSE improved?",
        (("exa pipeline promote",), ("--if-rmse-lt", "rmse")),
    ),
    Question(
        "reg-validate",
        "registry",
        "How do I smoke-test a model against its latency SLA before it goes live?",
        (("exa pipeline validate-model",),),
    ),
    Question(
        "reg-approve",
        "registry",
        "A model is waiting for sysadmin approval. How do I see and approve it?",
        (("exa approvals",),),
    ),
    # ── serving ───────────────────────────────────────────────────────────────
    Question(
        "serve-reload",
        "serving",
        "I promoted a new version but inference still returns the old one. What do I do?",
        (("exa serve reload",),),
    ),
    Question(
        "serve-check",
        "serving",
        # Re-worded 2026-08-28 (twice): the first phrasing was equally answered by
        # `exa pipeline validate-model --alias Production`, which really does send live
        # requests — but per model, and against a latency SLA. Asking about the deployment
        # itself was not enough either: the agent answered `exa status` / `exa doctor`, which
        # is a true answer to "is it up". Ruling the platform-wide check out in the question,
        # and asking for models-loaded + responses-returned, leaves only the serve-level probes.
        "The platform-wide status check looks fine, but I need to confirm the Ray Serve "
        "deployment specifically has its models loaded and is returning inference "
        "responses \u2014 which command does that?",
        (("exa serve check", "exa serve infer-check"),),
    ),
    Question(
        "serve-canary",
        "serving",
        "How do I send only 10% of traffic to a new version?",
        (("exa serve traffic",), ("canary", "10")),
    ),
    Question(
        "serve-ab",
        "serving",
        "How do I tell whether the canary is actually better, not just luckier?",
        (("exa serve ab",),),
    ),
    # ── drift & monitoring ────────────────────────────────────────────────────
    Question(
        "drift-status",
        "monitoring",
        "How do I check whether a model has drifted?",
        (("exa drift status",),),
    ),
    Question(
        "drift-baseline",
        "monitoring",
        "Drift says CRITICAL but the model is fine — the baseline is stale. How do I reset it?",
        (("exa drift baseline", "exa drift reset"),),
    ),
    Question(
        "drift-input",
        "monitoring",
        "How do I tell whether the *inputs* changed rather than the predictions?",
        (("exa drift input",),),
    ),
    Question(
        "drift-auto",
        "monitoring",
        "Can the platform retrain automatically when drift goes critical?",
        (("exa drift auto-retrain", "exa autopilot"),),
    ),
    # ── autopilot & governance ────────────────────────────────────────────────
    Question(
        "gov-autopilot",
        "governance",
        "What is the autopilot and how do I try it without it changing anything?",
        (("exa autopilot",), ("--dry-run", "dry run", "dry-run")),
    ),
    Question(
        "gov-audit",
        "governance",
        "Someone promoted a model last week. How do I find out who?",
        (("exa audit",),),
    ),
    Question(
        "gov-policy",
        "governance",
        "How do I stop a model being promoted unless it meets our rules?",
        (("exa policy", "exa eval gate", "exa pipeline promote"),),
    ),
    Question(
        "gov-judge",
        "governance",
        "Can I use an LLM judge to gate promotion?",
        (("calibrat",),),
        note="ADR 0111: an uncalibrated judge must refuse to gate, so any correct answer "
        "has to raise calibration rather than just say yes.",
    ),
    # ── HPC & cost ────────────────────────────────────────────────────────────
    Question(
        "hpc-clusters",
        "hpc",
        "Which HPC clusters can I actually submit to?",
        (("exa hpc clusters", "exa hpc nodes"),),
    ),
    Question(
        "hpc-place",
        "hpc",
        "I need 4 GPUs. Which cluster should the job go to?",
        (("exa hpc place",),),
    ),
    Question(
        "cost-model",
        "finops",
        "What has this model cost us in GPU-hours?",
        (("exa models cost", "exa finops"),),
    ),
    Question(
        "cost-carbon",
        "finops",
        "Can I report the carbon footprint of our training?",
        (("exa finops carbon",),),
    ),
)


@dataclass
class MentionsAll:
    """Score an answer by how many of *its own* expectation groups it satisfies.

    ``Regex`` in this package applies one pattern to every item; operator questions each carry
    their own expectation, so the evaluator reads it from the item's metadata.
    """

    metric: str = "operator_qa"
    questions: dict[str, Question] = field(default_factory=dict)

    def score(self, item: EvalItem) -> Score:
        qid = item.metadata.get("question_id", "")
        q = self.questions.get(qid)
        if q is None:
            return Score(self.metric, 0.0, {"error": f"unknown question {qid!r}"})
        missing = q.missing(item.output)
        total = len(q.must_mention) or 1
        return Score(
            self.metric,
            (total - len(missing)) / total,
            {"question_id": qid, "category": q.category, "missing": [list(g) for g in missing]},
        )


def by_id() -> dict[str, Question]:
    return {q.id: q for q in OPERATOR_QUESTIONS}


def to_items(answers: dict[str, str]) -> list[EvalItem]:
    """Turn ``{question_id: answer}`` into items ``run_suite``/``exa eval run`` can score."""
    qs = by_id()
    return [
        EvalItem(
            output=answers[qid],
            prompt=qs[qid].prompt,
            metadata={"question_id": qid, "category": qs[qid].category},
        )
        for qid in answers
        if qid in qs
    ]


# A command word must start with a letter — otherwise "exa --help" reads as a command
# named "--help" and the CLI-truth guard rejects a perfectly valid expectation.
_COMMAND = re.compile(r"exa [a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*")


def referenced_commands() -> set[str]:
    """Every ``exa …`` command string the expectations rely on, for the CLI-truth guard."""
    found: set[str] = set()
    for q in OPERATOR_QUESTIONS:
        for group in q.must_mention:
            for alt in group:
                for m in _COMMAND.finditer(alt.strip().strip("`")):
                    found.add(m.group(0))
    return found
